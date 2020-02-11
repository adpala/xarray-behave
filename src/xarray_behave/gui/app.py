"""PLOT SONG AND PLAY VIDEO IN SYNC

`python -m xarray_behave.ui datename root cuepoints`
"""
import os
import sys
import logging
import time
from pathlib import Path
from functools import partial
import warnings
import defopt

import numpy as np
import scipy
import skimage.draw
import scipy.interpolate

from pyqtgraph.Qt import QtGui, QtCore, QtWidgets
import pyqtgraph as pg

from .. import xarray_behave as xb
from .. import loaders as ld
from .. import _ui_utils
from .. import colormaps
from . import views

sys.setrecursionlimit(10**6)  # increase recursion limit to avoid errors when keeping key pressed for a long time

# currently, this is both model and controller
class PSV():

    MAX_AUDIO_AMP = 3.0

    def __init__(self, ds, vr=None, event_times=None, cue_points=[], cmap_name: str = 'turbo', box_size: int = 200,
                 fmin=None, fmax=None):
        pg.setConfigOptions(useOpenGL=False)   # appears to be faster that way
        # build model:
        self.ds = ds
        # self.m = views.Model(ds)
        self.vr = vr
        self.cue_points = cue_points
        self.event_times = event_times

        self.box_size = box_size
        self.fmin = fmin
        self.fmax = fmax

        if 'song' in self.ds:
            self.tmax = self.ds.song.shape[0]
        elif 'song_raw' in self.ds:
            self.tmax = self.ds.song_raw.shape[0]
        elif 'body_positions' in self.ds:
            # self.tmax = len(self.ds.body_positions) * 10  # TODO: get from  factor ds.attrs.body_positions.sampling_rate
            self.tmax = len(self.ds.body_positions) * ds.attrs['target_sampling_rate_Hz']
        else:
            raise ValueError('No time stamp info in dataset.')

        self.crop = True
        self.thorax_index = 8
        self.show_dot = True if 'body_positions' in self.ds else False
        self.old_show_dot_state = self.show_dot
        self.dot_size = 2
        self.show_poses = False
        self.move_poses = False
        self.circle_size = 8
        self.show_framenumber = False

        self.cue_index = 0

        self.nb_flies = np.max(self.ds.flies).values + 1 if 'flies' in self.ds.dims else 1
        self.focal_fly = 0
        self.other_fly = 1 if self.nb_flies > 1 else 0
        self.nb_bodyparts = len(self.ds.poseparts) if 'poseparts' in self.ds else 1

        self.fly_colors = _ui_utils.make_colors(self.nb_flies)
        self.bodypart_colors = _ui_utils.make_colors(self.nb_bodyparts)

        self.nb_eventtypes = len(self.ds.event_types)
        self.eventype_colors = _ui_utils.make_colors(self.nb_eventtypes)

        self.STOP = True
        self.swap_events = []
        self.show_spec = True
        self.spec_win = 200
        self.show_songevents = True
        self.show_manualonly = False
        self.movable_events = True
        self.move_only_current_events = True
        self.show_all_channels = True
        self.nb_channels = self.ds.song_raw.shape[-1]
        self.sinet0 = None
        # FIXME: will fail for datasets w/o song
        if 'song_events' in self.ds:
            self.fs_other = self.ds.song_events.attrs['sampling_rate_Hz']
        else:
            self.fs_other = ds.attrs['target_sampling_rate_Hz']

        if 'song' in self.ds:
            self.fs_song = self.ds.song.attrs['sampling_rate_Hz']
        elif 'song_raw' in self.ds:
            self.fs_song = self.ds.song_raw.attrs['sampling_rate_Hz']
        else:
            self.fs_song = self.fs_other  # not sure this would work? 
        
        if self.vr is not None:
            self.frame_interval = self.fs_song / self.vr.frame_rate  # song samples? TODO: get from self.ds
        else:
            self.frame_interval = self.fs_song / 1_000
        
        self._span = int(self.fs_song)
        self._t0 = int(self.span / 2)

        # build UI/controller
        self.app = pg.QtGui.QApplication([])
        self.win = pg.QtGui.QMainWindow()

        self.win.resize(1000, 800)
        self.win.setWindowTitle("psv")

        # build menu
        self.bar = self.win.menuBar()
        file = self.bar.addMenu("File")
        self.add_keyed_menuitem(file, "Save swap files", self.save_swaps)
        self.add_keyed_menuitem(file, "Save annotations", self.save_annotations)
        self.add_keyed_menuitem(file, "Save dataset", self.save_dataset)
        file.addSeparator()
        file.addAction("Exit")

        edit = self.bar.addMenu("Edit")
        self.add_keyed_menuitem(edit, "Swap flies", self.swap_flies, QtCore.Qt.Key_X)

        view_play = self.bar.addMenu("Playback")
        self.add_keyed_menuitem(view_play, "Play video", self.toggle_playvideo, QtCore.Qt.Key_Space,
                                checkable=True, checked=not self.STOP)
        view_play.addSeparator()
        self.add_keyed_menuitem(view_play, " < Reverse one frame", self.single_frame_reverse, QtCore.Qt.Key_Left),
        self.add_keyed_menuitem(view_play, "<< Reverse jump", self.jump_reverse, QtCore.Qt.Key_A)
        self.add_keyed_menuitem(view_play, ">> Forward jump", self.jump_forward, QtCore.Qt.Key_D)
        self.add_keyed_menuitem(view_play, " > Forward one frame", self.single_frame_advance, QtCore.Qt.Key_Right)
        view_play.addSeparator()
        self.add_keyed_menuitem(view_play, "Move to previous cue point", self.set_prev_cuepoint, QtCore.Qt.Key_K)
        self.add_keyed_menuitem(view_play, "Move to next cue point", self.set_next_cuepoint, QtCore.Qt.Key_L)
        view_play.addSeparator()
        self.add_keyed_menuitem(view_play, "Zoom in song", self.zoom_in_song, QtCore.Qt.Key_W)
        self.add_keyed_menuitem(view_play, "Zoom out song", self.zoom_out_song, QtCore.Qt.Key_S)
        view_play.addSeparator()
        self.add_keyed_menuitem(view_play, "Go to frame", self.go_to_frame, None)
        self.add_keyed_menuitem(view_play, "Go to time", self.go_to_time, None)

        view_video = self.bar.addMenu("Video")
        self.add_keyed_menuitem(view_video, "Crop frame", partial(self.toggle, 'crop'), QtCore.Qt.Key_C,
                                checkable=True, checked=self.crop)
        self.add_keyed_menuitem(view_video, "Change focal fly", self.change_focal_fly, QtCore.Qt.Key_F)
        view_video.addSeparator()
        self.add_keyed_menuitem(view_video, "Move poses", partial(self.toggle, 'move_poses'), QtCore.Qt.Key_B,
                                checkable=True, checked=self.move_poses)
        view_video.addSeparator()
        self.add_keyed_menuitem(view_video, "Show fly position", partial(self.toggle, 'show_dot'), QtCore.Qt.Key_O,
                                checkable=True, checked=self.show_dot)
        self.add_keyed_menuitem(view_video, "Show poses", partial(self.toggle, 'show_poses'), QtCore.Qt.Key_P,
                                checkable=True, checked=self.show_poses)
        self.add_keyed_menuitem(view_video, "Show framenumber", partial(self.toggle, 'show_framenumber'), None,
                                checkable=True, checked=self.show_framenumber)

        view_audio = self.bar.addMenu("Audio")
        self.add_keyed_menuitem(view_audio, "Play waveform as audio", self.play_audio, QtCore.Qt.Key_E)
        view_audio.addSeparator()
        self.add_keyed_menuitem(view_audio, "Show annotations", partial(self.toggle, 'show_songevents'), QtCore.Qt.Key_V,
                                checkable=True, checked=self.show_songevents)
        self.add_keyed_menuitem(view_audio, "Show manual annotations only", partial(self.toggle, 'show_manualonly'), QtCore.Qt.Key_I,
                                checkable=True, checked=self.show_manualonly)
        self.add_keyed_menuitem(view_audio, "Show all channels", partial(self.toggle, 'show_all_channels'), None,
                                checkable=True, checked=self.show_all_channels)
        view_audio.addSeparator()
        self.add_keyed_menuitem(view_audio, "Show spectrogram", partial(self.toggle, 'show_spec'), QtCore.Qt.Key_G,
                                checkable=True, checked=self.show_spec)
        self.add_keyed_menuitem(view_audio, "Increase frequency resolution", self.dec_freq_res, QtCore.Qt.Key_R)
        self.add_keyed_menuitem(view_audio, "Increase temporal resolution", self.inc_freq_res, QtCore.Qt.Key_T)
        view_audio.addSeparator()
        self.add_keyed_menuitem(view_audio, "Move events", partial(self.toggle, 'movable_events'), QtCore.Qt.Key_M,
                                checkable=True, checked=self.movable_events)
        self.add_keyed_menuitem(view_audio, "Move only selected events", partial(self.toggle, 'move_only_current_events'), None,
                                checkable=True, checked=self.move_only_current_events)
        self.add_keyed_menuitem(view_audio, "Delete events of selected type in view",
                                self.delete_current_events, QtCore.Qt.Key_U)
        self.add_keyed_menuitem(view_audio, "Delete all events in view", self.delete_all_events, QtCore.Qt.Key_Y)

        view_train = self.bar.addMenu("Training/Inference")
        self.add_keyed_menuitem(view_train, "Train", self.dss_train, None)
        self.add_keyed_menuitem(view_train, "Predict", self.dss_predict, None)
        
        self.bar.addMenu("View")

        self.hl = pg.QtGui.QHBoxLayout()

        # EVENT TYPE selector
        self.cb = pg.QtGui.QComboBox()
        self.cb.addItem("No annotation")
        self.eventList = [(cnt, evt) for cnt, evt in enumerate(self.ds.event_types.values) if 'manual' in evt]
        for event_type in self.eventList:
            self.cb.addItem("Add " + event_type[1])
        self.cb.currentIndexChanged.connect(self.update_xy)
        self.cb.setCurrentIndex(0)
        self.hl.addWidget(self.cb)

        # populate menu with event types so we can catch keys for them - allows changing event type for annotation using numeric keys
        view_audio.addSeparator()
        for ii in range(self.cb.count()):
            self.cb.itemText(ii)
            self.add_keyed_menuitem(view_audio,
                                    self.cb.itemText(ii),
                                    self.change_event_type,
                                    eval(f'QtCore.Qt.Key_{ii}'))

        # CHANNEL selector
        self.cb2 = pg.QtGui.QComboBox()
        if 'song' in self.ds:
            self.cb2.addItem("Merged channels")
        if 'song_raw' in self.ds:
            for chan in range(self.ds.song_raw.shape[1]):
                self.cb2.addItem("Channel " + str(chan))
        self.cb2.currentIndexChanged.connect(self.update_xy)
        self.cb2.setCurrentIndex(0)
        self.hl.addWidget(self.cb2)
        if self.vr is not None:
            self.movie_view = views.MovieView(model=self, callback=self.on_video_clicked)

        if cmap_name in colormaps.cmaps:
            colormap = colormaps.cmaps[cmap_name]
            colormap._init()
            lut = (colormap._lut * 255).view(np.ndarray)  # convert matplotlib colormap from 0-1 to 0 -255 for Qt
        else:
            logging.warning(f'Unknown colormap "{cmap_name}"" provided. Using default (turbo).')
        
        self.slice_view = views.TraceView(model=self, callback=self.on_trace_clicked)
        self.spec_view = views.SpecView(model=self, callback=self.on_trace_clicked, colormap=lut)

        self.cw = pg.GraphicsLayoutWidget()

        self.ly = pg.QtGui.QVBoxLayout()
        self.ly.addLayout(self.hl)
        if self.vr is not None:
            self.ly.addWidget(self.movie_view, stretch=4)
        self.ly.addWidget(self.slice_view, stretch=1)
        self.ly.addWidget(self.spec_view, stretch=1)

        self.cw.setLayout(self.ly)

        self.win.setCentralWidget(self.cw)
        self.win.show()

        self.update_xy()
        self.update_frame()

    @property
    def fs_ratio(self):
        return self.fs_song / self.fs_other

    @property
    def time0(self):
        return int(int(max(0, self.t0 - self.span / 2) / self.fs_ratio) * self.fs_ratio)

    @property
    def time1(self):
        return int(int(max(0, self.t0 + self.span / 2) / self.fs_ratio) * self.fs_ratio)

    @property
    def trange(self):
        return np.array([self.time0, self.time1]) / self.fs_song
        
    @property
    def t0(self):
        return self._t0

    @t0.setter
    def t0(self, val):
        self._t0 = np.clip(val, self.span / 2, self.tmax - self.span / 2)  # ensure t0 stays within bounds
        self.update_xy()
        self.update_frame()

    @property
    def framenumber(self):
        return self.ds.coords['nearest_frame'][self.index_other].data
        # return self.ds.nearest_frame.sel(time=self.ds.sampletime[int(self.t0)], method='nearest')

    @property
    def span(self):
        return self._span

    @span.setter
    def span(self, val):
        # HACK fixes weird offset/jump error - then probably arise from self.fs_song / self.fs_other
        self._span = min(max(200, val), self.tmax)
        self.update_xy()

    @property
    def span_index(self):
        return self.span / (2 * self.fs_song / self.fs_other)

    @property
    def current_event_index(self):
        if self.cb.currentIndex() - 1 < 0:
            return None
        else:
            return self.eventList[self.cb.currentIndex() - 1][0]

    @property
    def current_event_name(self):
        if self.current_event_index is None:
            return None
        else:
            return str(self.ds.event_types[self.current_event_index].values)

    @property
    def current_channel_name(self):
        return self.cb2.currentText()

    @property
    def current_channel_index(self):
        if self.current_channel_name != 'Merged channels':
            return int(self.current_channel_name.split(' ')[-1])  # "Channel XX"
        else:
            return None

    @property
    def index_other(self):
        return int(self.t0 * self.fs_other / self.fs_song)

    def add_keyed_menuitem(self, parent, label: str, callback, qt_keycode=None, checkable=False, checked=True):
        """Add new action to menu and register key press."""
        menuitem = parent.addAction(label)
        menuitem.setCheckable(checkable)
        menuitem.setChecked(checked)
        if qt_keycode is not None:
            menuitem.setShortcut(qt_keycode)
        menuitem.triggered.connect(lambda: callback(qt_keycode))
        return menuitem

    def change_event_type(self, qt_keycode):
        """Select event to annotate using key presses (0-nb_events)."""
        key_pressed = QtGui.QKeySequence(qt_keycode).toString()  # numeric key code to actual char pressed
        try:
            self.cb.setCurrentIndex(int(key_pressed))
        except ValueError:  # if non-int pressed or int too large for index
            pass

    def toggle(self, var_name, qt_keycode):
        self.__setattr__(var_name, not self.__getattribute__(var_name))
        if self.STOP:
            self.update_frame()
            self.update_xy()

    def delete_current_events(self, qt_keycode):
        if self.current_event_index is not None:
            self.ds.song_events.data[int(self.index_other - self.span_index):
                                     int(self.index_other + self.span_index), self.current_event_index] = False
            logging.info(f'   Deleted all {self.ds.event_types[self.current_event_index].values} in view.')
        else:
            logging.info(f'   No event type selected. Not deleting anything.')
        if self.STOP:
            self.update_xy()

    def delete_all_events(self, qt_keycode):
        self.ds.song_events.data[int(self.index_other - self.span_index):
                                 int(self.index_other + self.span_index), :] = False
        logging.info(f'   Deleted all events in view.')
        if self.STOP:
            self.update_xy()

    def inc_freq_res(self, qt_keycode):
        self.spec_win = int(self.spec_win * 2)
        if self.STOP:
            # need to update twice to fix axis limits for some reason
            self.update_xy()
            self.update_xy()

    def dec_freq_res(self, qt_keycode):
        self.spec_win = int(max(2, self.spec_win // 2))
        if self.STOP:
            # need to update twice to fix axis limits for some reason
            self.update_xy()
            self.update_xy()

    def toggle_playvideo(self, qt_keycode):
        self.STOP = not self.STOP
        if not self.STOP:
            self.play_video()

    def toggle_show_poses(self, qt_keycode):
        self.show_poses = not self.show_poses
        if self.show_poses:
            self.old_show_dot_state = self.show_dot
            self.show_dot = False
        else:
            self.show_dot = self.old_show_dot_state
        if self.STOP:
            self.update_frame()

    def change_focal_fly(self, qt_keycode):
        tmp = (self.focal_fly + 1) % self.nb_flies
        if tmp == self.other_fly:  # swap focal and other fly if same
            self.other_fly, self.focal_fly = self.focal_fly, self.other_fly
        else:
            self.focal_fly = tmp
        if self.STOP:
            self.update_frame()

    def set_prev_cuepoint(self, qt_keycode):
        if self.cue_points:
            self.cue_index = max(0, self.cue_index - 1)
            logging.debug(f'cue val at cue_index {self.cue_index} is {self.cue_points[self.cue_index]}')
            self.t0 = self.cue_points[self.cue_index]  # jump to PREV cue point

    def set_next_cuepoint(self, qt_keycode):
        if self.cue_points:
            self.cue_index = min(self.cue_index + 1, len(self.cue_points) - 1)
            logging.debug(f'cue val at cue_index {self.cue_index} is {self.cue_points[self.cue_index]}')
            self.t0 = self.cue_points[self.cue_index]  # jump to PREV cue point

    def zoom_in_song(self, qt_keycode):
        self.span /= 2

    def zoom_out_song(self, qt_keycode):
        self.span *= 2

    def single_frame_reverse(self, qt_keycode):
        self.t0 -= self.frame_interval

    def single_frame_advance(self, qt_keycode):
        self.t0 += self.frame_interval

    def jump_reverse(self, qt_keycode):
        self.t0 -= self.span / 2

    def jump_forward(self, qt_keycode):
        self.t0 += self.span / 2

    def go_to_frame(self, qt_keycode):
        fn, okPressed = QtGui.QInputDialog.getInt(self.win, "Enter frame number", "Frame number:", 
                            value=self.framenumber, min=0, max=np.max(self.ds.nearest_frame.data), step=1)
        if okPressed:
            time_index = np.argmax(self.ds.nearest_frame>fn)  
            self.t0 = int(time_index / self.fs_other * self.fs_song)
        
    def go_to_time(self, qt_keycode):
        time, okPressed = QtGui.QInputDialog.getDouble(self.win, "Enter time", "Seconds:", 
                value=self.t0 / self.fs_song, min=0, max=self.tmax /self.fs_song)
        if okPressed:
            self.t0 = np.argmax(self.ds.sampletime.data>time)
        
    def update_xy(self):
        self.x = self.ds.sampletime.data[self.time0:self.time1]
        self.step = int(max(1, np.ceil(len(self.x) / self.fs_song / 2)))  # make sure step is >= 1
        self.y_other = None
        
        if 'song' in self.ds and self.current_channel_name == 'Merged channels':
            self.y = self.ds.song.data[self.time0:self.time1]
        else:
            # load song for current channel
            try:
                self.y = self.ds.song_raw.data[self.time0:self.time1, self.current_channel_index].compute()
            except AttributeError:
                self.y = self.ds.song_raw.data[self.time0:self.time1, self.current_channel_index]

            if self.show_all_channels:
                channel_list = np.delete(np.arange(self.nb_channels), self.current_channel_index)
                self.y_other = self.ds.song_raw[self.time0:self.time1].values[:, channel_list]
                
        self.slice_view.update_trace()
        
        self.spec_view.clear_annotations()
        if self.show_spec:
            self.spec_view.update_spec(self.x, self.y)
        else:
            self.spec_view.clear()

        if self.show_songevents:
            self.plot_song_events(self.x)
        
        self.app.processEvents()

    def update_frame(self):
        if self.vr is not None:
            self.movie_view.update_frame()
            self.app.processEvents()

    def plot_song_events(self, x):
        for event_type in range(self.nb_eventtypes):
            movable = self.STOP and self.movable_events
            event_name = self.ds.event_types.values[event_type]
            if self.move_only_current_events:
                movable = movable and self.current_event_index==event_type

            if self.show_manualonly and 'manual' not in event_name:
                continue
            event_pen = pg.mkPen(color=self.eventype_colors[event_type], width=1)
            event_brush = pg.mkBrush(color=[*self.eventype_colors[event_type], 25])
            
            this = self.event_times[event_name]
            if self.ds.event_categories.data[event_type] == 'segment':
                event_onset_indices = this[np.logical_and(this[:,0]>x[0], this[:,0]<x[-1]), 0]
                event_offset_indices = this[np.logical_and(this[:,1]>x[0], this[:,1]<x[-1]), 1]
                # ensure onsets and offsets match
                # TODO plot partial segments
                if len(event_onset_indices) and len(event_offset_indices):
                    # breakpoint()
                    event_offset_indices = event_offset_indices[event_offset_indices>np.min(event_onset_indices)]
                    event_onset_indices = event_onset_indices[event_onset_indices<np.max(event_offset_indices)]
                    for onset, offset in zip(event_onset_indices, event_offset_indices):
                        self.slice_view.add_segment(onset, offset, event_type, event_brush, movable=movable)
                        if self.show_spec:
                            self.spec_view.add_segment(onset, offset, event_type, event_brush, movable=movable)            
            elif len(this):
                events = this[np.logical_and(this>x[0], this<x[-1])]
                if len(events):
                    self.slice_view.add_event(events, event_type, event_pen, movable=movable)
                    if self.show_spec:
                        self.spec_view.add_event(events, event_type, event_pen, movable=movable)
        
    def play_video(self):  # TODO: get rate from ds (video fps attr)
        RUN = True
        cnt = 0
        dt0 = time.time()
        while RUN:
            self.t0 += self.frame_interval
            cnt += 1
            if cnt % 10 == 0:
                # logging.debug(time.time() - dt0)
                dt0 = time.time()
            if self.STOP:
                RUN = False
                self.update_xy()
                self.update_frame()
                logging.debug('   Stopped playback.')

    def on_region_change_finished(self, region):    
        """Called when dragging a segment-like song_event - will change its bounds."""
        if self.move_only_current_events and self.current_event_index != region.event_index:
            return

        # this could be done by the view:
        f = scipy.interpolate.interp1d(region.xrange, self.trange, 
                                       bounds_error=False, fill_value='extrapolate')
        new_region = f(region.getRegion())
        # replace segment in events
        this = self.event_times[self.current_event_name]
        event_idx = np.where(np.logical_and(this[:,0]==region.bounds[0], 
                                            this[:,1]==region.bounds[1]))
        this[event_idx, :] = new_region    
        logging.info(f'  Moved {self.current_event_name} from t=[{region.bounds[0]:1.4f}:{region.bounds[1]:1.4f}] to [{new_region[0]:1.4f}:{new_region[1]:1.4f}] seconds.')
        self.update_xy()

    def on_position_change_finished(self, position):
        """Called when dragging an event-like song_event - will change time."""
        if self.move_only_current_events and self.current_event_index != position.event_index:
            return

        # this should be done by the view:
        # convert position to time coordinates (important for spec_view since x-axis does not correspond to time)
        f = scipy.interpolate.interp1d(position.xrange, self.trange, 
                                       bounds_error=False, fill_value='extrapolate')
        new_position = f(position.pos()[0])
        # set new position in ds
        this = self.event_times[self.current_event_name]
        this[np.where(this==position.position)] = new_position
        logging.info(f'  Moved {self.ds.event_types.values[position.event_index]} from t=[{position.position:1.4f} to {new_position:1.4f} seconds.')
        self.update_xy()

    def on_position_dragged(self, fly, pos, offset):
        """Called when dragging a fly body position - will change that pos."""
        pos0 = self.ds.pose_positions_allo.data[self.index_other, fly, self.thorax_index]
        pos1 = [pos.y(), pos.x()]
        self.ds.pose_positions_allo.data[self.index_other, fly, :] += (pos1 - pos0) 
        logging.info(f'   Moved fly from {pos0} to {pos1}.')
        self.update_frame()
        
    def on_poses_dragged(self, ind, pos, offset):
        """Called when dragging a fly body position - will change that pos."""
        # breakpoint()
        fly, part = np.unravel_index(ind, (self.nb_flies, self.nb_bodyparts))
        pos0 = self.ds.pose_positions_allo.data[self.index_other, fly, part]
        pos1 = [pos.y(), pos.x()]
        self.ds.pose_positions_allo.data[self.index_other, fly, part] += (pos1 - pos0) 
        logging.info(f'   Moved {self.ds.poseparts[part].data} of fly {fly} from {pos0} to {pos1}.')
        self.update_frame()
       
    def on_video_clicked(self, mouseX, mouseY):
        """Called when clicking the video - will select the focal fly."""
        fly_pos = self.ds.pose_positions_allo.data[self.index_other, :, self.thorax_index, :]
        fly_pos = np.array(fly_pos)  # in case this is a dask.array
        if self.crop:  # transform fly pos to coordinates of the cropped box
            box_center = self.ds.pose_positions_allo.data[self.index_other,
                                                          self.focal_fly,
                                                          self.thorax_index] + self.box_size / 2
            box_center = np.array(box_center)  # in case this is a dask.array
            fly_pos = fly_pos - box_center
        fly_dist = np.sum((fly_pos - np.array([mouseY, mouseX]))**2, axis=-1)
        fly_dist[self.focal_fly] = np.inf  # ensure that other_fly is not focal_fly
        self.other_fly = np.argmin(fly_dist)
        logging.debug(f"Selected {self.other_fly}.")
        self.update_frame()

    def on_trace_clicked(self, mouseT, mouseButton):
        """Called when traceview or specview have been clicked - will add new
        song event at click position.
        """
        if self.current_event_index is None:
            return
        if mouseButton == 1:  # add event
            if self.current_event_index is not None:
                if self.ds.event_categories.data[self.current_event_index] == 'segment':
                    if self.sinet0 is None:
                        self.spec_view.setCursor(QtGui.QCursor(QtCore.Qt.CrossCursor))
                        self.slice_view.setCursor(QtGui.QCursor(QtCore.Qt.CrossCursor))
                        self.sinet0 = mouseT
                    else:
                        self.spec_view.setCursor(QtGui.QCursor(QtCore.Qt.ArrowCursor))
                        self.slice_view.setCursor(QtGui.QCursor(QtCore.Qt.ArrowCursor))
                        self.event_times[self.current_event_name] = np.concatenate((self.event_times[self.current_event_name],
                                                                                    np.array(sorted([self.sinet0, mouseT]))[np.newaxis,:]),
                                                                                   axis=0)
                        logging.info(f'  Added {self.current_event_name} at t=[{self.sinet0:1.4f}:{mouseT:1.4f}] seconds.')
                        self.sinet0 = None
                else:  # pulse-like event
                    self.sinet0 = None
                    self.event_times[self.current_event_name] = np.append(self.event_times[self.current_event_name], mouseT)
                    logging.info(f'  Added {self.current_event_name} at t={mouseT:1.4f} seconds.')
                self.update_xy()
            else:
                self.sinet0 = None
        elif mouseButton == 2:  #delete nearest event
            self.sinet0 = None
            self.spec_view.setCursor(QtGui.QCursor(QtCore.Qt.ArrowCursor))
            self.slice_view.setCursor(QtGui.QCursor(QtCore.Qt.ArrowCursor))
            this = self.event_times[self.current_event_name]        
            if self.ds.event_categories.data[self.current_event_index] == 'segment':
                nearest_onset = float(_ui_utils.find_nearest(this[:,0], mouseT))
                event_idx = np.where(this[:, 0]==nearest_onset)[0]
                matching_offset = float(this[event_idx, 1])
                event_at_mouseT = matching_offset > mouseT
                if event_at_mouseT:
                    self.event_times[self.current_event_name] = np.delete(this, event_idx, axis=0)
                    logging.info(f'  Deleted {self.current_event_name} from {nearest_onset:1.4f} to {matching_offset:1.4f} seconds.')
            elif self.ds.event_categories.data[self.current_event_index] == 'event':
                tol = 0.05
                nearest_event = _ui_utils.find_nearest(this, mouseT)
                event_at_mouseT = np.abs(mouseT - nearest_event) < tol
                if event_at_mouseT:
                    self.event_times[self.current_event_name] = np.delete(this, np.where(this==nearest_event)[0])
                    logging.info(f'  Deleted {self.current_event_name} at t={nearest_event:1.4f} seconds.')
            self.update_xy()
                    
    def play_audio(self, qt_keycode):
        """Play vector as audio using the simpleaudio package."""

        if 'song' in self.ds or 'song_raw' in self.ds:
            try:
                import simpleaudio
            except (ImportError, ModuleNotFoundError):
                logging.info('Could not import simpleaudio. Maybe you need to install it.\
                              See https://simpleaudio.readthedocs.io/en/latest/installation.html for instructions.')
                return

            if 'song' in self.ds and self.current_channel_name == 'Merged channels':
                y = self.ds.song.data[self.time0:self.time1]
            else:
                y = self.ds.song_raw.data[self.time0:self.time1, self.current_channel_index]
            y = np.array(y)  # if y is a dask.array (lazy loaded)

            # normalize to 16-bit range and convert to 16-bit data
            max_amp = self.MAX_AUDIO_AMP
            if max_amp is None:
                max_amp = np.nanmax(np.abs(y))
            y = y * 32767 / max_amp
            y = y.astype(np.int16)
            # simpleaudio can only play at these rates - choose the one nearest to our rate
            allowed_sample_rates = [8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000, 192000]  # Hz
            sample_rate = min(allowed_sample_rates, key=lambda x: abs(x - int(self.fs_song)))
            # FIXME resample audio to new sample_rate to preserve sound
            # start playback in background
            simpleaudio.play_buffer(y, num_channels=1, bytes_per_sample=2, sample_rate=sample_rate)
        else:
            logging.info(f'Could not play sound - no sound data in the dataset.')

    def swap_flies(self, qt_keycode):
        if self.vr is not None:
            # use swap_dims to swap flies in all dataarrays in the data set?
            # TODO make sure this does not fail for datasets w/o song
            logging.info(f'   Swapping flies 1 & 2 at time {self.t0}.')
            # if already in there remove - swapping a second time would negate first swap
            if [self.index_other, self.focal_fly, self.other_fly] in self.swap_events:
                self.swap_events.remove([self.index_other, self.focal_fly, self.other_fly])
            else:
                self.swap_events.append([self.index_other, self.focal_fly, self.other_fly])

            self.ds.pose_positions_allo.values[self.index_other:, [
                self.other_fly, self.focal_fly], ...] = self.ds.pose_positions_allo.values[self.index_other:, [self.focal_fly, self.other_fly], ...]
            self.ds.pose_positions.values[self.index_other:, [
                self.other_fly, self.focal_fly], ...] = self.ds.pose_positions.values[self.index_other:, [self.focal_fly, self.other_fly], ...]
            self.ds.body_positions.values[self.index_other:, [
                self.other_fly, self.focal_fly], ...] = self.ds.body_positions.values[self.index_other:, [self.focal_fly, self.other_fly], ...]
            self.update_frame()

    def save_swaps(self, qt_keycode):
        savefilename = Path(self.ds.attrs['root'], self.ds.attrs['res_path'],
                            self.ds.attrs['datename'], f"{self.ds.attrs['datename']}_idswaps_test.txt")
        savefilename, _ = QtWidgets.QFileDialog.getSaveFileName(self.win, 'Save swaps to', str(savefilename),
                                                                filter="txt files (*.txt);;all files (*)")
        if len(savefilename):
            logging.info(f'   Saving list of swap indices to {savefilename}.')
            np.savetxt(savefilename, self.swap_events, fmt='%d', header='index fly1 fly2')
            logging.info(f'   Done.')

    def save_annotations(self, qt_keycode):
        logging.info('   Updating song events')
        self.ds = _ui_utils.eventtimes_to_traces(self.ds, self.event_times)
        savefilename = Path(self.ds.attrs['root'], self.ds.attrs['res_path'], self.ds.attrs['datename'],
                            f"{self.ds.attrs['datename']}_songmanual.zarr")
        savefilename, _ = QtWidgets.QFileDialog.getSaveFileName(self.win, 'Save annotations to', str(savefilename),
                                                                filter="zarr files (*.zarr);;all files (*)")
        if len(savefilename):
            logging.info(f'   Saving annotations to {savefilename}.')
            # currently, can only save datasets as zarr - so convert song_events data array to dataset before saving
            xb.save(savefilename, self.ds.song_events.to_dataset())
            logging.info(f'   Done.')

    def save_dataset(self, qt_keycode):
        logging.info('   Updating song events')
        self.ds = _ui_utils.eventtimes_to_traces(self.ds, self.event_times)
        savefilename = Path(self.ds.attrs['root'], self.ds.attrs['dat_path'], self.ds.attrs['datename'],
                            f"{self.ds.attrs['datename']}.zarr")
        savefilename, _ = QtWidgets.QFileDialog.getSaveFileName(self.win, 'Save dataset to', str(savefilename),
                                                                filter="zarr files (*.zarr);;all files (*)")
        if len(savefilename):
            logging.info(f'   Saving dataset to {savefilename}.')
            # currently, can only save datasets as zarr - so convert song_events data array to dataset before saving
            xb.save(savefilename, self.ds)
            logging.info(f'   Done.')

    def dss_train(self, qt_keycode):
        logging.info('Training not implemented yet.')
        # export dataset as training/testing files
        # call dss.train

    def dss_predict(self, qt_keycode):
        logging.info('Prediction not implemented yet.')
        # load model
        # predict
        # update ds
        

def main(datename: str, *,
         root: str = '', dat_path='dat', res_path='res',
         ignore_existing: bool = False, lazy: bool = False,
         create_manual_segmentation: bool = False,        
         save: bool = False, savefolder: str = '',
         resample_video_data: bool = True, target_sampling_rate: int = 10_000,
         with_song: bool = True, cue_points: str = '[]',
         spec_freq_min = None, spec_freq_max = None,
         box_size: int = 200, cmap_name: str = 'turbo'):
    """
    Args:
        datename (str): Experiment id.
                        If "datename" or "datename.zarr" exists: Open dataset file with that name.
                        Otherwise: Experiment id - will assemble new dataset from files in 
                        root/dat_path/datename and root/res_path/datename.
        root (str): Path containing the `dat` and `res` folders for the experiment. 
                    Defaults to '' (current directory).
        dat_path (str): Subdirectory in root holding the experiment data. 
                        Defaults to 'dat'.
        res_path (str): Subdirectory in root holding the tracking etc results. 
                        Defaults to 'res'.        
        ignore_existing (bool): Ignore existing dataset file. 
                                Forces assembly of dataset even if "datename.zarr" exists. 
                                Defaults to False.
        create_manual_segmentation (bool): Create empty segmentation data structure for annotating song.
                                    Song types currently default to ['sine_manual', 'pulse_manual', 'vibration_manual', 'aggression_manual']
                                    Defaults to False.
        lazy (bool): Whether to load full dataset into memory or read on demand from disk 
                     (only applicable if opening an existing dataset).
                     This can greatly speeds up loading - the dataset opens much more quickly
                     but this comes at the price of slower access to the data
                     while using the GUI. Use if you only want to quickly check things, 
                     but not if you plan to annotate the full data set.
                     Defaults to False.
        save (bool): Save to the newly assembled dataset as a zarr.ZipStore. 
                     Slows down assembly but useful if you plan to work with the dataset again 
                     since loading is faster than assembly (and can be done lazily).
                     Use judiciously to avoid cluttering and filling up the data storage.
        savefolder (str): Folder in which created dataset to save to. Defaults to ''.
        with_song (bool): whether or not to include song data
        cue_points (str): List of cue points (indices) for quickly jumping around time. Defaults to '[]'.
        target_sampling_rate (int): Sampling rate for the pose data and sound annotations. Defaults to 10_000 Hz.
        resample_video_data (bool): Whether to resample the video data to the target_sampling_rate 
                                    or to the sample grid defined by each frame.
                                    Useful for checking poses, since this will preserve accurate frame-by-frame positions.
                                    Defaults to True. 
        box_size (int): Size of the crop box around flies, in pixels Defaults to 200px.
        spec_freq_min (int): Smallest frequency to display in the spectrogram view. 
                             Defaults to None (smallest possible frequency in the spectrogram).
        spec_freq_max (int): Greatest frequency to display in the spectrogram view. 
                             Defaults to None (greatest possible frequency in the spectrogram).
        cmap_name (str): Name of the colormap (one of ['magma', 'inferno', 'plasma', 'viridis', 'parula', 'turbo']). 
                         Defaults to 'turbo'.
    """
    if os.path.exists(datename + '.zarr') or os.path.exists(datename):
        is_ds = True
        loadname = datename if os.path.exists(datename) else datename+'.zarr'
    else:
        is_ds = False
        
    if not ignore_existing and is_ds:  #os.path.exists(datename + '.zarr'):
        logging.info(f'Loading ds from {loadname}.')
        ds = xb.load(loadname, lazy=True)
        if 'song_events' in ds:
            ds.song_events.load()  # never lazy load song events so we can edit them
        if not lazy:
            logging.info(f'   Loading data from ds.')
            if 'song' in ds:
                ds.song.load()  # non-lazy load song for faster updates
            if 'pose_positions_allo' in ds:
                ds.pose_positions_allo.load()  # non-lazy load song for faster updates
            if 'sampletime' in ds:
                ds.sampletime.load()
            if 'song_raw' in ds:  # this will take a long time:    
                ds.song_raw.load()  # non-lazy load song for faster updates
    else:
        logging.info(f'Assembling dataset for {datename}.')
        ds = xb.assemble(datename, root=root, dat_path='dat', res_path='res', 
                         fix_fly_indices=False, include_song=with_song,
                         keep_multi_channel=True, target_sampling_rate=target_sampling_rate,
                         resample_video_data=resample_video_data)
        if save:
            logging.info('   saving dataset.')
            xb.save(savefolder + datename + '.zarr', ds)

    if create_manual_segmentation or 'song_events' not in ds:
        ds = ld.initialize_manual_song_events(ds, from_segmentation=False, force_overwrite=False)

    # add event categories if they are missing in the dataset
    if 'event_categories' not in ds:
        event_categories = ['segment' 
                            if 'sine' in evt or 'syllable' in evt 
                            else 'event' 
                            for evt in ds.event_types.values]  
        ds = ds.assign_coords({'event_categories': 
                               (('event_types'),
                               event_categories)}) 

    # detect all event times and segment on/offsets
    if 'song_events' in ds:
        event_times = _ui_utils.detect_events(ds)
    else:
        event_times = dict()
    logging.info(ds)
    filepath = ds.attrs['video_filename']
    vr = None
    try:
        try:
            video_filename = filepath[:-3] + 'mp4'
            vr = _ui_utils.VideoReaderNP(video_filename)
        except:
            video_filename = filepath[:-3] + 'avi'
            vr = _ui_utils.VideoReaderNP(video_filename)
        logging.info(vr)
    except FileNotFoundError:
        logging.info(f'Video "{video_filename}" not found. Continuing without.')
    except:
        logging.info(f'Something went wrong when loading the video. Continuing without.')

    cue_points = eval(cue_points)
    PSV(ds, vr, event_times, cue_points, cmap_name, box_size=box_size, fmin=spec_freq_min, fmax=spec_freq_max)

    # Start Qt event loop unless running in interactive mode or using pyside.
    if (sys.flags.interactive != 1) or not hasattr(QtCore, 'PYQT_VERSION'):
        QtGui.QApplication.instance().exec_()


if __name__ == '__main__':
    import warnings
    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.INFO)
    defopt.run(main)
