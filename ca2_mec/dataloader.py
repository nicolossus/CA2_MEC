"""
This file contains code for loading spike trains and tracking data.

There are three main functions one might want to use from here:
    1. load_spiketrains(action_id)
    2. load_tracking_data(action_id)
    3. load_head_direction(action_id)

This file takes inspiration from the septum-mec project by Mikkel Lepperød,
but makes it easier to use (subjective opinion). In particular, that the functions
for loading data only require the action_id as an argument. Moreover, the spikes
are stored as neo.SpikeTrain objects with metadata stored in the annotations.
"""

import os
import pathlib
import quantities as pq
import neo
import pandas as pd
import numpy as np
from expipe_plugin_cinpla.data_loader import (
    load_epochs, get_channel_groups, load_spiketrains, load_unit_annotations,
    load_leds, get_duration, load_lfp, get_sample_rate, get_data_path
)
import spikeextractors as se
import expipe
from utils import *


def in_brain_regions(spikes, brain_regions=["ca2"]):
    """
    Returns a reduced set of spikes that are in the given brain regions.

    Parameters
    ----------
    spikes : list of neo.SpikeTrain
    brain_regions : list of brain region strings

    Returns
    -------
    list of neo.SpikeTrain
    """
    new_spikes = []
    for sptr in spikes:
        tetrode_id = 't' + str(sptr.annotations["group_id"])
        for tag in sptr.annotations["tags"]:
            if tag.split("=")[0] == tetrode_id and tag.split("=")[1] in brain_regions:
                new_spikes.append(sptr)
                break
    return new_spikes


def persistent_units(spikes, persistent_actions):
    """
    Returns a reduced set of spikes that are persistent in the given actions.

    Parameters
    ----------
    spikes : list of neo.SpikeTrain
    persistent_actions : list of action-strings

    Returns
    -------
    list of neo.SpikeTrain
    """
    return [
        spike_train
        for spike_train in spikes
        if set(spike_train.annotations["persistent_actions"]) == set(persistent_actions)
    ]


def persistent_trials(spikes, persistent_trials):
    """
    Returns a reduced set of spikes that are persistent in the given trials.

    Parameters
    ----------
    spikes : list of neo.SpikeTrain
    persistent_trials : list of trial-strings

    Returns
    -------
    list of neo.SpikeTrain
    """
    return [
        spike_train
        for spike_train in spikes
        if set(persistent_trials).issubset(
            set(spike_train.annotations["persistent_trials"])
        )
    ]


def project_path():
    path = pathlib.Path("/projects/ec109/maria/mec_social")
    return path


def action_path(action_id):
    return project_path() / "actions" / action_id / "data" / "main.exdir"


def get_duration(data_path):
    f = exdir.File(str(data_path), "r", plugins=[exdir.plugins.quantities])

    return f.attrs["session_duration"].rescale("s")


def load_spiketrains(
    action_id,
    load_spikes=True,
    channel_group=None,
    load_waveforms=False,
    lim=None,
    identify_neurons=False,
):
    """
    Parameters
    ----------
    data_path
    channel_group
    load_waveforms
    remove_label
    Returns
    -------
    """
    # load expipe-action
    project = expipe.get_project(project_path())
    action = project.require_action(action_id)
    if identify_neurons:
        identify_neurons_df = project.require_action("identify-neurons")
        identify_neurons_df = pd.read_csv(identify_neurons_df.data_path() / "units.csv")

    sorting = se.ExdirSortingExtractor(
        action_path(action_id),
        channel_group=channel_group,
        load_waveforms=load_waveforms,
    )
    mua_df = load_action_mua(action_id)
    cluster_info_df = load_group_id(action_id)
    sptr = []
    # build neo objects
    for u in sorting.get_unit_ids():
        times = (
            sorting.get_unit_spike_train(u) / (sorting.get_sampling_frequency() * pq.Hz)
            if load_spikes
            else [] * pq.s
        )
        if lim is None:
            t_stop = get_duration(action_path(action_id))
            t_start = 0 * pq.s
        else:
            t_start = pq.Quantity(lim[0], "s")
            t_stop = pq.Quantity(lim[1], "s")
        mask = (times >= t_start) & (times <= t_stop)
        times = times[mask]
        if load_waveforms and "waveforms" in sorting.get_unit_spike_feature_names(u):
            wf = sorting.get_unit_spike_features(u, "waveforms")
            wf = wf[mask] * pq.uV
        else:
            wf = None
        st = neo.SpikeTrain(
            times=times,
            t_stop=t_stop,
            waveforms=wf,
            sampling_rate=sorting.get_sampling_frequency() * pq.Hz,
        )
        for p in sorting.get_unit_property_names(u):
            if p == "group_id":
                unit_id = int(sorting.get_unit_property(u, 'name').split("#")[-1])
                group_id = cluster_info_df.loc[cluster_info_df['id'] == unit_id,'ch_group']
                st.annotations.update({p: group_id.values[0]})
            else:
                st.annotations.update({p: sorting.get_unit_property(u, p)})

        # set unit name to int
        st.annotations.update({"name": int(st.annotations["name"].split("#")[-1])})
        st.annotations["unit_name"] = st.annotations.pop(
            "name"
        )  # rename name to unit_name
        st.annotations.update({"action_id": action_id})

        # add MUA info
        try:
            mua_id, mua_quality = mua_df.loc[
                mua_df["cluster_id"] == st.annotations["unit_name"]
            ].values[0]
        except IndexError:
            mua_quality = "unassigned"
        st.annotations.update({"mua_quality": mua_quality})

        # add expipe attributes
        st.annotations.update(action.attributes)

        # add custom annotations
        st.annotations.update({"trial_id": trial_identity(action)})

        # add identify_neurons info
        if identify_neurons:
            add_identify_neurons(st, identify_neurons_df)

        sptr.append(st)

    return sptr


def correct_mua(sptr, only_good_mua=False):
    """
    Corrects MUA_quality to be consistent across trials.
    In other words, if a unit is set to "bad" MUA-quality, it will be set to
    "good" if it is also set to "good" in any other trial.
    """
    for spike_train in sptr:
        if spike_train.annotations["mua_quality"] != "good":
            for spike_train2 in sptr:
                if (
                    spike_train2.annotations["unit_id"]
                    == spike_train.annotations["unit_id"]
                ) and spike_train2.annotations["mua_quality"] == "good":
                    spike_train.annotations["mua_quality"] = "good"
                    break

    if only_good_mua:
        sptr = [st for st in sptr if st.annotations["mua_quality"] == "good"]

    return sptr


def load_action_mua(action_id):
    mua_path = (
        action_path(action_id)
        / "processing/electrophysiology/spikesorting/mountainsort4/phy/cluster_group.tsv"
    )
    df = pd.read_csv(mua_path, sep="\t")
    return df

def load_group_id(action_id):
    # group_id is the same as ch_group ---- of course...
    group_id_path = (
        action_path(action_id)
        / "processing/electrophysiology/spikesorting/mountainsort4/phy/cluster_info.tsv"
    )
    cluster_group_pd_table = pd.read_csv(group_id_path, sep="\t")
    # if 'ch_group' is nan for some units, infer them based on 'ch'
    nan_ch_groups = cluster_group_pd_table['ch_group'].isnull()
    cluster_group_pd_table.loc[nan_ch_groups, 'ch_group'] = cluster_group_pd_table.loc[nan_ch_groups, 'ch'] // 4
    return cluster_group_pd_table

def add_identify_neurons(spike_train, identify_neurons):
    """
    Adds identify_neurons to all spike trains

    Parameters
    ----------
    spike_train : neo.SpikeTrain
    identify_neurons : pandas.DataFrame
    """
    unit_id_mask = (
        identify_neurons["action"] == spike_train.annotations["action_id"]
    ) & (identify_neurons["unit_name"] == spike_train.annotations["unit_name"])
    spike_train.annotations.update(
        {"unit_id": identify_neurons["unit_id"][unit_id_mask].item()}
    )
    spike_train.annotations.update(
        {"unit_idnum": identify_neurons["unit_idnum"][unit_id_mask].item()}
    )
    persistent_mask = (
        identify_neurons["unit_id"] == identify_neurons["unit_id"][unit_id_mask].item()
    )
    persistent_actions = identify_neurons["action"][persistent_mask].values
    spike_train.annotations.update({"persistent_actions": persistent_actions})
    spike_train.annotations.update(
        {"persistent_trials": [trial_identity(action) for action in persistent_actions]}
    )


def velocity_filter(x, y, t, threshold):
    """
    Removes values above threshold
    Parameters
    ----------
    x : quantities.Quantity array in m
        1d vector of x positions
    y : quantities.Quantity array in m
        1d vector of y positions
    t : quantities.Quantity array in s
        1d vector of times at x, y positions
    threshold : float
    """
    assert len(x) == len(y) == len(t), "x, y, t must have same length"
    vel = np.gradient([x, y], axis=1) / np.gradient(t)
    speed = np.linalg.norm(vel, axis=0)
    speed_mask = speed < threshold
    speed_mask = np.append(speed_mask, 0)
    x = x[np.where(speed_mask)]
    y = y[np.where(speed_mask)]
    t = t[np.where(speed_mask)]
    return x, y, t


def interp_filt_position(x, y, tm, fs=100, f_cut=10):
    """
    rapid head movements will contribute to velocity artifacts,
    these can be removed by low-pass filtering
    see http://www.ncbi.nlm.nih.gov/pmc/articles/PMC1876586/
    code addapted from Espen Hagen
    Parameters
    ----------
    x : quantities.Quantity array in m
        1d vector of x positions
    y : quantities.Quantity array in m
        1d vector of y positions
    tm : quantities.Quantity array in s
        1d vector of times at x, y positions
    fs : quantities scalar in Hz
        return radians
    Returns
    -------
    out : angles, resized t
    """
    import scipy.signal as ss

    assert len(x) == len(y) == len(tm), "x, y, t must have same length"
    t = np.arange(tm.min(), tm.max() + 1.0 / fs, 1.0 / fs)
    x = np.interp(t, tm, x)
    y = np.interp(t, tm, y)
    # rapid head movements will contribute to velocity artifacts,
    # these can be removed by low-pass filteringpar
    # see http://www.ncbi.nlm.nih.gov/pmc/articles/PMC1876586/
    # code addapted from Espen Hagen
    b, a = ss.butter(N=1, Wn=f_cut * 2 / fs)
    # zero phase shift filter
    x = ss.filtfilt(b, a, x)
    y = ss.filtfilt(b, a, y)
    # we tolerate small interpolation errors
    x[(x > -1e-3) & (x < 0.0)] = 0.0
    y[(y > -1e-3) & (y < 0.0)] = 0.0

    return x, y, t


def rm_nans(*args):
    """
    Removes nan from all corresponding arrays
    Parameters
    ----------
    args : arrays, lists or quantities which should have removed nans in
           all the same indices
    Returns
    -------
    out : args with removed nans
    """
    nan_indices = []
    for arg in args:
        nan_indices.extend(np.where(np.isnan(arg))[0].tolist())
    nan_indices = np.unique(nan_indices)
    out = []
    for arg in args:
        out.append(np.delete(arg, nan_indices))
    return out


# def load_leds(data_path):
#     root_group = exdir.File(data_path, "r", plugins=[exdir.plugins.quantities])

#     # tracking data
#     position_group = root_group["processing"]["tracking"]["camera_0"]["Position"]
#     stop_time = position_group.attrs["stop_time"]
#     x1, y1 = position_group["led_0"]["data"].data.T
#     t1 = position_group["led_0"]["timestamps"].data
#     x2, y2 = position_group["led_1"]["data"].data.T
#     t2 = position_group["led_1"]["timestamps"].data

#     return x1, y1, t1, x2, y2, t2, stop_time


def filter_xy_zero(x, y, t):
    (idxs,) = np.where((x == 0) & (y == 0))
    return [np.delete(a, idxs) for a in [x, y, t]]


def filter_xy_box_size(x, y, t, box_size):
    (idxs,) = np.where((x > box_size[0]) | (x < 0) | (y > box_size[1]) | (y < 0))
    return [np.delete(a, idxs) for a in [x, y, t]]


def filter_t_zero_duration(x, y, t, duration):
    (idxs,) = np.where((t < 0) | (t > duration))
    return [np.delete(a, idxs) for a in [x, y, t]]


def _cut_to_same_len(*args):
    out = []
    lens = []
    for arg in args:
        lens.append(len(arg))
    minlen = min(lens)
    for arg in args:
        out.append(arg[:minlen])
    return out


def load_head_direction(
    action_id, lim=None, sampling_rate=100, low_pass_frequency=6, box_size=[1.0, 1.0]
):
    from head_direction.head import head_direction

    data_path = action_path(action_id)

    x1, y1, t1, x2, y2, t2, stop_time = load_leds(data_path)
    x1, y1, t1 = rm_nans(x1, y1, t1)
    x2, y2, t2 = rm_nans(x2, y2, t2)
    x1, y1, t1 = filter_t_zero_duration(x1, y1, t1, stop_time.magnitude)
    x2, y2, t2 = filter_t_zero_duration(x2, y2, t2, stop_time.magnitude)

    # OE saves 0.0 when signal is lost, these can be removed
    x1, y1, t1 = filter_xy_zero(x1, y1, t1)
    x2, y2, t2 = filter_xy_zero(x2, y2, t2)

    # x1, y1, t1 = filter_xy_box_size(x1, y1, t1, box_size)
    # x2, y2, t2 = filter_xy_box_size(x2, y2, t2, box_size)

    x1, y1, t1 = interp_filt_position(
        x1, y1, t1, fs=sampling_rate, f_cut=low_pass_frequency
    )
    x2, y2, t2 = interp_filt_position(
        x2, y2, t2, fs=sampling_rate, f_cut=low_pass_frequency
    )
    x1, y1, t1, x2, y2, t2 = _cut_to_same_len(x1, y1, t1, x2, y2, t2)
    check_valid_tracking(x1, y1, box_size)
    check_valid_tracking(x2, y2, box_size)
    angles, times = head_direction(x1, y1, x2, y2, t1)

    # truncate to selected interval
    if lim is not None:
        mask = (times >= lim[0]) & (times <= lim[1])
        angles, times = angles[mask], times[mask]
    return np.stack([angles, times], axis=-1)


def check_valid_tracking(x, y, box_size):
    if np.isnan(x).any() and np.isnan(y).any():
        raise ValueError(
            "nans found in  position, "
            + "x nans = %i, y nans = %i" % (sum(np.isnan(x)), sum(np.isnan(y)))
        )

    if x.min() < 0 or x.max() > box_size[0] or y.min() < 0 or y.max() > box_size[1]:
        warnings.warn(
            "Invalid values found "
            + "outside box: min [x, y] = [{}, {}], ".format(x.min(), y.min())
            + "max [x, y] = [{}, {}]".format(x.max(), y.max())
        )


def ca2_tracking_transformation(x, y):
    """
    Transforms spatial-coordinates to align with open-ephys GUI coordinates
    in one of the lab rooms where CA2-project is recorded (2020-2023 ish)

    Transform is verified with recording test data in each of the corners of
    the box.
    """
    return x, 1 - y


def load_tracking(
    action_id,
    lim=None,
    sampling_rate=100,
    low_pass_frequency=6,
    box_size=[1.0, 1.0],
    velocity_threshold=5,
    ca2_transform_data=False,
):
    data_path = action_path(action_id)

    x1, y1, t1, x2, y2, t2, stop_time = load_leds(data_path)
    x1, y1, t1 = rm_nans(x1, y1, t1)
    x2, y2, t2 = rm_nans(x2, y2, t2)
    x1, y1, t1 = filter_t_zero_duration(x1, y1, t1, stop_time.magnitude)
    x2, y2, t2 = filter_t_zero_duration(x2, y2, t2, stop_time.magnitude)

    # select data with least nan
    if len(x1) > len(x2):
        x, y, t = x1, y1, t1
    else:
        x, y, t = x2, y2, t2

    # OE saves 0.0 when signal is lost, these can be removed
    x, y, t = filter_xy_zero(x, y, t)
    # x, y, t = filter_xy_box_size(x, y, t, box_size)

    # remove velocity artifacts
    x, y, t = velocity_filter(x, y, t, velocity_threshold)
    x, y, t = interp_filt_position(x, y, t, fs=sampling_rate, f_cut=low_pass_frequency)
    check_valid_tracking(x, y, box_size)

    vel = np.gradient([x, y], axis=1) / np.gradient(t)
    speed = np.linalg.norm(vel, axis=0)

    # truncate to selected interval
    if lim is not None:
        mask = (t >= lim[0]) & (t <= lim[1])
        x, y, t, speed = x[mask], y[mask], t[mask], speed[mask]

    if ca2_transform_data:
        x, y = ca2_tracking_transformation(x, y)

    return np.stack([x, y, t, speed], axis=-1)
