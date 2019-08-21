"""Create self-documenting xarray dataset from behavioral recordings and annotations."""
import numpy as np
import scipy.interpolate
import xarray as xr
import zarr
import logging
from pathlib import Path
from . import loaders as ld
from . import metrics as mt


def assemble(datename, root='', dat_path='dat', res_path='res', target_sampling_rate=1000,
             keep_multi_channel: bool = False, resample_video_data: bool = True):
    """Assemble data set containing song and video data.

    Synchronizes A/V, resamples annotations (pose, song) to common sampling grid.

    Args:
        datename
        root = ''
        dat_path = 'dat'
        res_path = 'res'
        target_sampling_rate [float=1000] Sampling rate in Hz for pose and annotation data.
        keep_multi_channel = False add multi-channel data (otherwise will only add merged traces)
    Returns
        xarray Dataset containing
            [DESCRIPTION]
    """

    # load RECORDING and TIMING INFORMATION
    filepath_timestamps = Path(root, dat_path, datename, f'{datename}_timeStamps.h5')
    filepath_daq = Path(root, dat_path, datename, f'{datename}_daq.h5')
    ss, last_sample_number, sampling_rate = ld.load_times(filepath_timestamps, filepath_daq)

    # LOAD TRACKS
    with_tracks = False
    with_fixed_tracks = False
    filepath_tracks = Path(root, res_path, datename, f'{datename}_tracks_fixed.h5')
    filepath_tracks_nonfixed = Path(root, res_path, datename, f'{datename}_tracks.h5')
    try:
        body_pos, body_parts, first_tracked_frame, last_tracked_frame, background = ld.load_tracks(filepath_tracks)
        with_tracks = True
        with_fixed_tracks = True
    except Exception as e:
        logging.info(f'Could not load tracks from {filepath_tracks}.')
        logging.debug(e)
        try:
            logging.info(f'Trying non-fixed tracks at {filepath_tracks_nonfixed}.')
            body_pos, body_parts, first_tracked_frame, last_tracked_frame, background = ld.load_tracks(filepath_tracks_nonfixed)
            with_tracks = True
        except Exception as e:
            logging.info(f'   This failed, too:')
            logging.debug(e)
            first_tracked_frame = int(ss.frame(0))
            last_tracked_frame = int(ss.frame(last_sample_number))
            logging.info(f'Setting first/last tracked frame numbers to those of the first/last sample in the recording ({first_tracked_frame}, {last_tracked_frame}).')
            
    # LOAD POSES from DEEPPOSEKIT
    with_poses = False
    poses_from = None
    filepath_poses = Path(root, res_path, datename, f'{datename}_poses_dpk.zarr')
    try:
        pose_pos, pose_pos_allo, pose_parts, first_pose_frame, last_pose_frame = ld.load_poses_deepposekit(filepath_poses)
        with_poses = True
        poses_from = 'DeepPoseKit'
    except Exception as e:
        logging.info(f'Could not load pose from {filepath_poses}.')
        logging.debug(e)

    # LOAD POSES from LEAP
    if not with_poses:
        filepath_poses = Path(root, res_path, datename, f'{datename}_poses.h5')
        try:
            pose_pos, pose_pos_allo, pose_parts, first_pose_frame, last_pose_frame = ld.load_poses_leap(filepath_poses)
            with_poses = True
            poses_from = 'LEAP'
        except Exception as e:
            logging.info(f'Could not load pose from {filepath_poses}.')
            logging.debug(e)

    # load AUTOMATIC SEGMENTATION - currently produced by matlab
    with_segmentation = False
    with_song = False
    res = dict()
    filepath_segmentation = Path(root, res_path, datename, f'{datename}_song.mat')
    try:
        res = ld.load_segmentation(filepath_segmentation)
        with_segmentation = True
        with_song = True
    except Exception as e:
        logging.info(f'Could not load segmentation from {filepath_segmentation}.')
        logging.debug(e)

    # load RAW song traces
    if not with_song or keep_multi_channel:
        try:
            logging.info(f'Reading recording from {filepath_daq}.')
            song_raw = ld.load_raw_song(filepath_daq)
            if not with_song:
                song_merged_max = ld.merge_channels(song_raw, sampling_rate)
                res['song'] = song_merged_max
            if keep_multi_channel:
                res['song_raw'] = song_raw
            with_song = True
        except Exception as e:
            logging.info(f'Could not load song from {filepath_daq}.')
            logging.debug(e)

    # load MANUAL SONG ANNOTATIONS
    with_segmentation_manual = False
    filepath_segmentation_manual = Path(root, res_path, datename, f'{datename}_songmanual.mat')
    try:
        manual_events_seconds = ld.load_manual_annotation(filepath_segmentation_manual)
        with_segmentation_manual = True
    except Exception as e:
        logging.info(f'Could not load manual segmentation from {filepath_segmentation_manual}.')
        logging.debug(e)

    last_sample_with_frame = np.min((last_sample_number, ss.sample(frame=last_tracked_frame - 1))).astype(np.intp)
    first_sample = 0
    last_sample = int(last_sample_with_frame)

    step = int(sampling_rate / target_sampling_rate)  # ms - will resample song annotations and tracking data to 1000Hz
    # construct desired sample grid for resampled data
    target_samples = np.arange(first_sample, last_sample, step, dtype=np.uintp)
    time = target_samples / sampling_rate  # time in seconds for each sample in the song annotation data
    # time in seconds for each sample in the song recording
    sampletime = np.arange(first_sample, last_sample) / sampling_rate

    # get nearest frame for each sample in the resampled grid
    frame_numbers = np.arange(first_tracked_frame, last_tracked_frame)
    frame_samples = ss.sample(frame_numbers)
    interpolator = scipy.interpolate.interp1d(frame_samples, frame_numbers,
                                              kind='nearest', bounds_error=False, fill_value=np.nan)
    nearest_frame_time = interpolator(target_samples).astype(np.uintp)

    dataset_data = dict()
    if with_song:  # MERGED and RAW song recording
        song = xr.DataArray(data=res['song'][first_sample:last_sample, 0],  # cut recording to match new grid
                            dims=['sampletime'],
                            coords={'sampletime': sampletime, },
                            attrs={'description': 'Song signal merged across all recording channels.',
                                   'sampling_rate_Hz': sampling_rate,
                                   'time_units': 'seconds',
                                   'amplitude_units': 'volts'})
        dataset_data['song'] = song

        if keep_multi_channel:
            song_raw = xr.DataArray(data=res['song_raw'][first_sample:last_sample, :],  # cut recording to match new grid
                                    dims=['sampletime', 'channels'],
                                    coords={'sampletime': sampletime, },
                                    attrs={'description': 'Raw song recording (multi channel).',
                                           'sampling_rate_Hz': sampling_rate,
                                           'time_units': 'seconds',
                                           'amplitude_units': 'volts'})
            dataset_data['song_raw'] = song_raw

    # SONG EVENTS
    if with_segmentation_manual:
        manual_events_samples = {key: (val * sampling_rate).astype(np.uintp)
                                 for key, val in manual_events_seconds.items()}
        # make mask for events that are defined by start/end points (sine)
        for key, val in manual_events_samples.items():
            if val.shape[1] == 2:  # val contains start/end points of each event
                mask = [np.arange(t0, t1+1, dtype=np.uintp) for (t0, t1) in val]
                manual_events_samples[key] = np.concatenate(mask)
        events = manual_events_samples
    else:
        events = dict()

    if with_segmentation:
        events['song_pulse_any'] = res['pulse_times_samples']
        events['song_pulse_slow'] = res['pulse_times_samples'][res['pulse_labels'] == 1]
        events['song_pulse_fast'] = res['pulse_times_samples'][res['pulse_labels'] == 0]
        events['sine'] = np.where(song_labels == 2)[0]

    if with_segmentation_manual or with_segmentation:
        eventtypes = [*events.keys()]
        song_events_np = np.zeros((len(time), len(eventtypes)), dtype=np.bool)  # pre-allocate grid holding event data
        for cnt, key in enumerate(events.keys()):
            event_times = np.unique((events[key] / step).astype(np.uintp))
            event_times = event_times[event_times < last_sample_with_frame / step]
            song_events_np[event_times, cnt] = True

        song_events = xr.DataArray(data=song_events_np,
                                   dims=['time', 'event_types'],
                                   coords={'time': time,
                                           'event_types': eventtypes,
                                           'nearest_frame': (('time'), nearest_frame_time), },
                                   attrs={'description': 'Event times as boolean arrays.',
                                          'sampling_rate_Hz': sampling_rate / step,
                                          'time_units': 'seconds', })
        dataset_data['song_events'] = song_events

    # BODY POSITION
    if with_tracks:
        frame_times = ss.frame_time(frame_numbers)
        fps = 1/np.nanmean(np.diff(frame_times))

        if resample_video_data:  # resample to common grid at target_sampling_rate.
            frame_numbers = np.arange(first_tracked_frame, last_tracked_frame)
            frame_samples = ss.sample(frame_numbers)  # get sample numbers for each frame

            interpolator = scipy.interpolate.interp1d(
                frame_samples, body_pos, axis=0, bounds_error=False, fill_value=np.nan)
            body_pos = interpolator(target_samples)
        else:
            time = frame_times
            nearest_frame_time = frame_numbers

        positions = xr.DataArray(data=body_pos,
                                 dims=['time', 'flies', 'bodyparts', 'coords'],
                                 coords={'time': time,
                                         'bodyparts': body_parts,
                                         'nearest_frame': (('time'), nearest_frame_time),
                                         'coords': ['y', 'x']},
                                 attrs={'description': 'coords are "allocentric" - rel. to the full frame',
                                        'sampling_rate_Hz': sampling_rate / step,
                                        'time_units': 'seconds',
                                        'video_fps': fps,
                                        'spatial_units': 'pixels',
                                        'background': background,
                                        'tracks_fixed': with_fixed_tracks})
        dataset_data['body_positions'] = positions

    # POSES
    if with_poses:
        frame_times = ss.frame_time(frame_numbers)
        fps = 1/np.nanmean(np.diff(frame_times))

        if resample_video_data:  # resample to common grid at target_sampling_rate.
            frame_numbers = np.arange(first_pose_frame, last_pose_frame)
            frame_samples = ss.sample(frame_numbers)  # get sample numbers for each frame
        
            interpolator = scipy.interpolate.interp1d(
                frame_samples, pose_pos, axis=0, kind='linear', bounds_error=False, fill_value=np.nan)
            pose_pos = interpolator(target_samples)
        
            interpolator = scipy.interpolate.interp1d(
                frame_samples, pose_pos_allo, axis=0, kind='linear', bounds_error=False, fill_value=np.nan)
            pose_pos_allo = interpolator(target_samples)
        else:
            time = frame_times
            nearest_frame_time = frame_numbers
            
        # poses in EGOCENTRIC coordinates
        poses = xr.DataArray(data=pose_pos,
                             dims=['time', 'flies', 'poseparts', 'coords'],
                             coords={'time': time,
                                     'poseparts': pose_parts,
                                     'nearest_frame': (('time'), nearest_frame_time),
                                     'coords': ['y', 'x']},
                             attrs={'description': 'coords are "egocentric" - rel. to box',
                                    'sampling_rate_Hz': sampling_rate / step,
                                    'time_units': 'seconds',
                                    'video_fps': fps,
                                    'spatial_units': 'pixels',
                                    'poses_from': poses_from})
        dataset_data['pose_positions'] = poses

        # poses in ALLOcentric (frame-relative) coordinates
        poses_allo = xr.DataArray(data=pose_pos_allo,
                                  dims=['time', 'flies', 'poseparts', 'coords'],
                                  coords={'time': time,
                                          'poseparts': pose_parts,
                                          'nearest_frame': (('time'), nearest_frame_time),
                                          'coords': ['y', 'x']},
                                  attrs={'description': 'coords are "allocentric" - rel. to frame',
                                         'sampling_rate_Hz': sampling_rate / step,
                                         'time_units': 'seconds',
                                         'video_fps': fps,
                                         'spatial_units': 'pixels',
                                         'poses_from': poses_from})
        dataset_data['pose_positions_allo'] = poses_allo

    # MAKE THE DATASET
    dataset = xr.Dataset(dataset_data, attrs={})
    # save command line args
    dataset.attrs = {'video_filename': str(Path(root, dat_path, datename, f'{datename}.mp4')),
                     'datename': datename, 'root': root, 'dat_path': dat_path, 'res_path': res_path}
    return dataset


def assemble_metrics(dataset, make_abs: bool = True, make_rel: bool = True, smooth_positions: bool = True):
    """[summary]
    
    Args:
        dataset ([type]): [description]
        make_abs (bool, optional): [description]. Defaults to True.
        make_rel (bool, optional): [description]. Defaults to True.
        smooth_positions (bool, optional): [description]. Defaults to True.
    
    Returns:
        [type]: [description]
        xarray.Dataset: containing these features:
                angles [time,flies]
                vels [time,flies,forward/lateral]
                chamber_vels [time,flies,y/x]
                rotational_speed [time,flies]
                accelerations [time,flies,forward/lateral]
                chamber_acc [time,flies,y/x]
                rotational_acc [time,flies]
                wing_angle_left [time,flies]
                wing_angle_right [time,flies]
                wing_angle_sum [time,flies]
                relative angle [time,flies,flies]
                (relative orientation) [time,flies,flies]
                (relative velocities) [time,flies,flies,y/x]
    """

    time = dataset.time
    nearest_frame_time = dataset.nearest_frame
    sampling_rate = dataset.pose_positions.attrs['sampling_rate_Hz']
    frame_rate = dataset.pose_positions.attrs['video_fps']

    thoraces = dataset.pose_positions_allo.loc[:, :, 'thorax', :].values.astype(np.float32)
    heads = dataset.pose_positions_allo.loc[:, :, 'head', :].values.astype(np.float32)
    wing_left = dataset.pose_positions_allo.loc[:, :, 'left_wing', :].values.astype(np.float32)
    wing_right = dataset.pose_positions_allo.loc[:, :, 'right_wing', :].values.astype(np.float32)


    if smooth_positions:
        # Smoothing window should span 2 frames to get smooth acceleration traces. 
        # Since std of Gaussian used for smoothing has std of winlen/8, winlen should span 16 frames.
        winlen = np.ceil(16 / frame_rate * sampling_rate)
        thoraces = mt.smooth(thoraces, winlen)
        heads = mt.smooth(heads, winlen)
        wing_left = mt.smooth(wing_left, winlen)
        wing_right = mt.smooth(wing_right, winlen)

    angles = mt.angle(thoraces, heads)
    chamber_vels = mt.velocity(thoraces, ref='chamber')

    ds_dict = dict()
    if make_abs:
        vels = mt.velocity(thoraces, heads)
        accelerations = mt.acceleration(thoraces, heads)
        chamber_acc = mt.acceleration(thoraces, ref='chamber')

        vels_x = chamber_vels[..., 1]
        vels_y = chamber_vels[..., 0]
        vels_forward = vels[..., 0]
        vels_lateral = vels[..., 1]
        vels_mag = np.linalg.norm(vels, axis=2)
        accs_x = chamber_acc[..., 1]
        accs_y = chamber_acc[..., 0]
        accs_forward = accelerations[..., 0]
        accs_lateral = accelerations[..., 1]
        accs_mag = np.linalg.norm(accelerations, axis=2)

        rotational_speed = mt.rot_speed(thoraces, heads)
        rotational_acc = mt.rot_acceleration(thoraces, heads)

        wing_angle_left = mt.angle(heads, thoraces) - mt.angle(thoraces, wing_left)
        wing_angle_right = -(mt.angle(heads, thoraces) - mt.angle(thoraces, wing_right))
        wing_angle_sum = wing_angle_left + wing_angle_right

        list_absolute = [
            angles, rotational_speed, rotational_acc,
            vels_mag, vels_x, vels_y, vels_forward, vels_lateral,
            accs_mag, accs_x, accs_y, accs_forward, accs_lateral,
            wing_angle_left, wing_angle_right, wing_angle_sum
        ]

        abs_feature_names = [
            'angles', 'rotational_speed', 'rotational_acceleration',
            'velocity_magnitude', 'velocity_x', 'velocity_y', 'velocity_forward', 'velocity_lateral',
            'acceleration_mag', 'acceleration_x', 'acceleration_y', 'acceleration_forward', 'acceleration_lateral',
            'wing_angle_left', 'wing_angle_right', 'wing_angle_sum'
        ]

        absolute = np.stack(list_absolute, axis=2)

        ds_dict['abs_features'] = xr.DataArray(data=absolute,
                                               dims=['time', 'flies', 'absolute_features'],
                                               coords={'time': time,
                                                       'absolute_features': abs_feature_names,
                                                       'nearest_frame': (('time'), nearest_frame_time)},
                                               attrs={'description': 'coords are "egocentric" - rel. to box',
                                                      'sampling_rate_Hz': sampling_rate,
                                                      'time_units': 'seconds',
                                                      'spatial_units': 'pixels'})
    if make_rel:
        # RELATIVE FEATURES #
        dis = mt.distance(thoraces)
        rel_angles = mt.relative_angle(thoraces, heads)
        rel_orientation = angles[:, np.newaxis, :] - angles[:, :, np.newaxis]
        rel_velocities_forward = chamber_vels[..., 0][:, np.newaxis, :] - chamber_vels[..., 0][:, :, np.newaxis]
        rel_velocities_lateral = chamber_vels[..., 1][:, np.newaxis, :] - chamber_vels[..., 1][:, :, np.newaxis]
        rel_velocities_mag = np.sqrt(rel_velocities_forward**2 + rel_velocities_lateral**2)

        list_relative = [
            dis, rel_angles, rel_orientation,
            rel_velocities_mag, rel_velocities_forward, rel_velocities_lateral
        ]

        rel_feature_names = [
            'distance', 'relative_angle', 'relative_orientation',
            'relative_velocity_mag', 'relative_velocity_forward', 'relative_velocity_lateral'
        ]

        relative = np.stack(list_relative, axis=3)
        ds_dict['rel_features'] = xr.DataArray(data=relative,
                                               dims=['time', 'flies', 'relative_flies', 'relative_features'],
                                               coords={'time': time,
                                                       'relative_features': rel_feature_names,
                                                       'nearest_frame': (('time'), nearest_frame_time)},
                                               attrs={'description': 'coords are "egocentric" - rel. to box',
                                                      'sampling_rate_Hz': sampling_rate,
                                                      'time_units': 'seconds',
                                                      'spatial_units': 'pixels'})

    # MAKE ONE DATASET
    feature_dataset = xr.Dataset(ds_dict, attrs={})
    return feature_dataset


def save(savepath, dataset):
    """[summary]

    Args:
        savepath ([type]): [description]
        dataset ([type]): [description]
    """
    with zarr.ZipStore(savepath, mode='w') as zarr_store:
        dataset.to_zarr(store=zarr_store, compute=True)


def _normalize_strings(dataset):
    """Ensure all keys in coords are proper (unicode?) python strings, not byte strings."""
    dn = dict()
    for key, val in dataset.coords.items():
        if val.dtype == 'S16':
            val.data = np.array([v.decode() for v in val.data])
        dn[key] = val
    return dataset


def load(savepath, lazy: bool = False, normalize_strings: bool = True):
    """[summary]
    
    Args:
        savepath ([type]): [description]
        lazy (bool, optional): [description]. Defaults to True.
        normalize_strings (bool, optional): [description]. Defaults to True.
    
    Returns:
        [type]: [description]
    """
    zarr_store = zarr.ZipStore(savepath, mode='r')
    dataset = xr.open_zarr(zarr_store)
    if not lazy:
        dataset.load()
        zarr_store.close()

    if normalize_strings:
        dataset = _normalize_strings(dataset)

    return dataset
