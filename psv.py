"""PLOT SONG AND PLAY VIDEO IN SYNC

`python plot_song_video.py datename root cuepoints`

datename: experiment name (e.g. localhost-20181120_144618)
root: defaults to the current directory - this will work if you're in #Common/chainingmic
cuepoints: string evaluating to a list (e.g. '[100, 50000, 100000]' - including the quotation ticks)
Keys (may need to "activate" plokeyst by clicking on song trace first to make this work):
W/A - move left/right
S/D - zoom in/out
K/L - jump to previous/next cue point
M - toggle mark thorax each fly with colored dot
P - toggle mark all tracked body parts with color dots
C - toggle crop video around position fly
F - next fly for cropping
X - swap first with second fly for all frames following the current frame 
O - save swap indices to file
SPACE - play/stop video/song trace in
"""
# DONE- IN PROGRESS: capture mouse events to select swap flies in case there are more than 2 flies (fly1 could be focal fly from crop, fly2 determined by mouse click position) 
#       see http://www.pyqtgraph.org/documentation/graphicsscene/mouseclickevent.html
# DONE: use defopt for catching/processing cmdline args
# DONE: remove horrible global vars and global scope!


import os
import sys
import logging
from pyqtgraph.Qt import QtGui, QtCore
import pyqtgraph as pg
import xarray_behave as xb
import numpy as np
from videoreader import VideoReader
from pathlib import Path
import cv2
import defopt
from functools import partial


def make_colors(nb_flies):
    colors = np.zeros((1, nb_flies, 3), np.uint8)
    colors[0, :, 1:] = 220  # set saturation and brightness to 220
    colors[0, :, 0] = np.arange(0, 180, 180.0 / nb_flies)  # set range of hues
    colors = cv2.cvtColor(colors, cv2.COLOR_HSV2BGR)[0].astype(np.uint8)[..., ::-1]
    return colors


class VideoReaderNP(VideoReader):
    """VideoReader posing as numpy array."""

    def __getitem__(self, index):
        return self.read(index)[1]

    @property
    def dtype(self):
        return np.uint8

    @property
    def shape(self):
        return (self.number_of_frames, *self.frame_shape)

    @property
    def ndim(self):
        return len(self.shape)

    @property
    def size(self):
        return np.product(self.shape)

    def min(self):
        return 0

    def max(self):
        return 255

    def transpose(self, *args):
        return self


class ImageViewVR(pg.ImageView):

    def quickMinMax(self, data):
        """Dummy min/max for numpy videoreader. The Original function tries to read the full video!"""
        return 0, 255


class KeyPressWidget(pg.GraphicsLayoutWidget):
    sigKeyPress = QtCore.pyqtSignal(object)
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def keyPressEvent(self, ev):
        self.scene().keyPressEvent(ev)
        self.sigKeyPress.emit(ev)


class PSV():

    BOX_SIZE = 200

    def __init__(self, ds, vr, cue_points=[]):
        pg.setConfigOptions(useOpenGL=False)   # appears to be faster that way
        self.ds = ds
        self.vr = vr
        self.cue_points = cue_points

        self.span = 100_000
        self.t0 = int(self.span/2) #2_800_000  # 1_100_000
        if hasattr(self.ds, 'song'):
            self.tmax = len(self.ds.song)
        else:
            self.tmax = len(self.ds.body_positions) * 10  # TODO: get factor from self.ds

        self.crop = False
        self.show_dot = True
        self.old_show_dot_state = self.show_dot
        self.show_poses = False
        self.focal_fly = 0
        self.other_fly = 1
        self.cue_index = 0
        self.nb_flies = np.max(self.ds.flies).values+1
        self.fly_colors = make_colors(self.nb_flies)
        self.nb_bodyparts = len(self.ds.poseparts)
        self.bodypart_colors = make_colors(self.nb_bodyparts)
        self.STOP = True
        self.swap_events = []
        self.mousex, self.mousey = None, None
        self.frame_interval = 100  # TODO: get from self.ds
        # FIXME: will fail for datasets w/o song
        self.fs_song = self.ds.song.attrs['sampling_rate_Hz']
        self.fs_other = self.ds.song_events.attrs['sampling_rate_Hz']

        self.app = pg.QtGui.QApplication([])
        self.win = pg.QtGui.QMainWindow()
        self.win.resize(800, 800)
        self.win.setWindowTitle("psv")

        self.image_view = ImageViewVR(name="img_view")
        self.image_view.setImage(self.vr)
        self.image_view.getImageItem().mouseClickEvent = self.click

        self.slice_view = pg.PlotWidget(name="song")
        
        self.cw = KeyPressWidget()
        self.cw.sigKeyPress.connect(self.keyPressed)
        
        self.buttonP = QtGui.QPushButton("[p]oses")
        self.buttonP.clicked.connect(partial(self.synthetic_key, key=QtCore.Qt.Key_P))
        self.buttonM = QtGui.QPushButton("[m]ark thorax")
        self.buttonM.clicked.connect(partial(self.synthetic_key, key=QtCore.Qt.Key_M))
        self.buttonC = QtGui.QPushButton("[c]rop frame")
        self.buttonC.clicked.connect(partial(self.synthetic_key, key=QtCore.Qt.Key_C))
        self.buttonK = QtGui.QPushButton("next [K] cue point")
        self.buttonK.clicked.connect(partial(self.synthetic_key, key=QtCore.Qt.Key_K))
        self.buttonL = QtGui.QPushButton("previous [L] cue point")
        self.buttonL.clicked.connect(partial(self.synthetic_key, key=QtCore.Qt.Key_L))
        self.buttonX = QtGui.QPushButton("[x]change fly labels")
        self.buttonX.clicked.connect(partial(self.synthetic_key, key=QtCore.Qt.Key_X))
        self.buttonO = QtGui.QPushButton("save [O] exchange points")
        self.buttonO.clicked.connect(partial(self.synthetic_key, key=QtCore.Qt.Key_O))

        self.hl = QtGui.QHBoxLayout()
        self.hl.addWidget(self.buttonP, stretch=1)
        self.hl.addWidget(self.buttonM, stretch=1)
        self.hl.addWidget(self.buttonC, stretch=1)
        self.hl.addWidget(self.buttonK, stretch=1)
        self.hl.addWidget(self.buttonL, stretch=1)
        self.hl.addWidget(self.buttonX, stretch=1)
        self.hl.addWidget(self.buttonO, stretch=1)
        
        self.ly = pg.QtGui.QVBoxLayout()
        self.ly.addLayout(self.hl)
        self.ly.addWidget(self.image_view, stretch=4)
        self.ly.addWidget(self.slice_view, stretch=1)
        self.cw.setLayout(self.ly)
        self.win.setCentralWidget(self.cw)
        self.win.show()
        self.image_view.ui.histogram.hide()
        self.image_view.ui.roiBtn.hide()
        self.image_view.ui.menuBtn.hide()
        self.update()


    def keyPressed(self, evt):
        # global t0, span, crop, fly, STOP, dot, cue_index, span
        if evt.key() == QtCore.Qt.Key_Left:  # go one frame back
            self.t0 -= self.frame_interval  # TODO get val from fps
        elif evt.key() == QtCore.Qt.Key_Right:  # advance by one frame
            self.t0 += self.frame_interval  # TODO get val from fps
        if evt.key() == QtCore.Qt.Key_A:
            self.t0 -= self.span / 2
        elif evt.key() == QtCore.Qt.Key_D:
            self.t0 += self.span / 2
        elif evt.key() == QtCore.Qt.Key_W:
            self.span /= 2
        elif evt.key() == QtCore.Qt.Key_S:
            self.span *= 2
        elif evt.key() == QtCore.Qt.Key_P:
            self.show_poses = not self.show_poses
            if self.show_poses:
                self.old_show_dot_state = self.show_dot
                self.show_dot = False
            else:
                self.show_dot = self.old_show_dot_state
        elif evt.key() == QtCore.Qt.Key_M:
            self.show_dot = not self.show_dot
        elif evt.key() == QtCore.Qt.Key_K:
            if self.cue_points:
                self.cue_index = max(0, self.cue_index-1)
                logging.debug(f'cue val at cue_index {self.cue_index} is {self.cue_points[self.cue_index]}')
                # t0 = self.ds.time[cue_points[cue_index]].values  # jump to PREV cue point
                self.t0 = self.cue_points[self.cue_index]  # jump to PREV cue point
        elif evt.key() == QtCore.Qt.Key_L:
            if self.cue_points:
                self.cue_index = min(self.cue_index+1, len(self.cue_points)-1)
                logging.debug(f'cue val at cue_index {self.cue_index} is {self.cue_points[self.cue_index]}')
                self.t0 = self.cue_points[self.cue_index]  # jump to PREV cue point
        elif evt.key() == QtCore.Qt.Key_X:
            self.ds, self.swap_events = swap_flies(self.ds, self.t0, self.swap_events)  # make this class method
            logging.info(f'   Swapping flies 1 & 2 at time {self.t0}.')
        elif evt.key() == QtCore.Qt.Key_O:
            savefilename = Path(self.ds.attrs['root'], self.ds.attrs['res_path'], self.ds.attrs['datename'], f"{self.ds.attrs['datename']}_idswaps_test.txt")
            save_swap_events(savefilename, self.swap_events)
            logging.info(f'   Saving list of swap indices to {savefilename}.')
        elif evt.key() == QtCore.Qt.Key_C:
            self.crop = not self.crop
            logging.info(f'   Cropping = {self.crop}.')
        elif evt.key() == QtCore.Qt.Key_F:
            self.focal_fly = (self.focal_fly+1) % self.nb_flies
            logging.info(f'   Cropping around fly {self.focal_fly}.')
        elif evt.key() == QtCore.Qt.Key_Space:
            if self.STOP:
                logging.info(f'   Starting playback.')
                self.STOP = False
                self.play()
            else:
                logging.info(f'   Stopping playback.')
                self.STOP = True

        self.span = int(max(500, self.span))
        self.t0 = int(np.clip(self.t0, self.span/2, self.tmax - self.span/2))
        if self.STOP:  # avoid unecessary update if playback is running
            self.update()
        self.app.processEvents()


    def update(self):
        """Updates the view."""
        if hasattr(self.ds, 'song'):
            index_other = int(self.t0 * self.fs_other / self.fs_song)

            # clear trace plot and update with new trace
            time0 = int(self.t0 - self.span / 2)
            time1 = int(self.t0 + self.span / 2)

            x = self.ds.sampletime[time0:time1].values
            y = self.ds.song[time0:time1].values
            step = int(np.ceil(len(x) / self.fs_song / 2))
            self.slice_view.clear()
            self.slice_view.plot(x[::step], y[::step])

            # # mark song events in trace
            vibration_indices = np.where(self.ds.song_events[int(
                index_other - self.span / 20):int(index_other + self.span / 20), 0].values)[0] * int(1 / self.fs_other * self.fs_song)
            vibrations = x[vibration_indices]
            for vib in vibrations:
                self.slice_view.addItem(pg.InfiniteLine(movable=False, angle=90, pos=vib))

            # indicate time point of displayed frame in trace
            self.slice_view.addItem(pg.InfiniteLine(movable=False, angle=90,
                                            pos=x[int(self.span / 2)], pen=pg.mkPen(color='r', width=1)))
        else:
            index_other = int(self.t0 * self.fs_other / 10_000)
        fn = self.ds.body_positions.nearest_frame[index_other]
        frame = self.vr[fn]
        thorax_index = 8
        dot_size = 2
        if self.show_poses:
            for dot_fly in range(self.nb_flies):
                fly_pos = self.ds.pose_positions_allo[index_other, dot_fly, ...].values
                x_dot = np.clip((fly_pos[..., 0]-dot_size, fly_pos[..., 0]+dot_size), 0, self.vr.frame_width-1).astype(np.uintp)
                y_dot = np.clip((fly_pos[..., 1]-dot_size, fly_pos[..., 1]+dot_size), 0, self.vr.frame_height-1).astype(np.uintp)
                for bodypart_color, x_pos, y_pos in zip(self.bodypart_colors, x_dot.T, y_dot.T):
                    frame[slice(*x_pos), slice(*y_pos), :] = bodypart_color  # now crop frame around one of the flies

        if self.show_dot:
            for dot_fly in range(self.nb_flies):
                fly_pos = self.ds.pose_positions_allo[index_other, dot_fly, thorax_index].values
                x_dot = np.clip((fly_pos[0]-dot_size, fly_pos[0]+dot_size), 0, self.vr.frame_width-1).astype(np.uintp)
                y_dot = np.clip((fly_pos[1]-dot_size, fly_pos[1]+dot_size), 0, self.vr.frame_height-1).astype(np.uintp)
                frame[slice(*x_dot), slice(*y_dot), :] = self.fly_colors[dot_fly]  # set pixels around 

        if self.crop:
            fly_pos = self.ds.pose_positions_allo[index_other, self.focal_fly, thorax_index]
            # makes sure crop does not exceed frame bounds
            x_range = np.clip((fly_pos[0]-self.BOX_SIZE, fly_pos[0]+self.BOX_SIZE), 0, self.vr.frame_width-1).astype(np.uintp)
            y_range = np.clip((fly_pos[1]-self.BOX_SIZE, fly_pos[1]+self.BOX_SIZE), 0, self.vr.frame_height-1).astype(np.uintp)
            # now crop frame around the focal fly
            frame = frame[slice(*x_range), slice(*y_range), :]
        self.image_view.clear()
        self.image_view.setImage(frame)
        self.app.processEvents()

    def play(self, rate=100):  # TODO: get rate from ds (video fps attr)
        RUN = True
        while RUN:
            self.t0 += rate
            self.update()
            self.app.processEvents()
            if self.STOP:
                RUN = False

    def click(self, event):
        event.accept()
        pos = event.pos()
        # logging.debug(f'raw pos x={int(pos.x())}, y={int(pos.y())}.')
        self.mousex, self.mousey = int(pos.x()), int(pos.y())
        # mousepos = self.image_view.view.mapSceneToView(pos)  # get mouse po
        # self.mousex, self.mousey = int(mousepos.x()), int(mousepos.y())
        logging.debug(f'mouse clicked at x={self.mousex}, y={self.mousey}.')
        # find nearest fly
        thorax_index = 8
        index_other = int(self.t0 * self.fs_other / self.fs_song)
        fly_pos = self.ds.pose_positions_allo[index_other, :, thorax_index, :].values
        if self.crop:  # transform mouse pos to coordinates of the cropped box
            fly_pos = fly_pos - self.ds.pose_positions_allo[index_other, self.focal_fly, thorax_index].values + self.BOX_SIZE/2
        
        logging.debug(np.round(fly_pos))
        fly_dist = np.sum((fly_pos - np.array([self.mousex, self.mousey]))**2, axis=-1)
        fly_dist[self.focal_fly] = np.inf  # ensure that other_fly is not focal_fly
        self.other_fly = np.argmin(fly_dist)
        logging.debug(f"Selected {self.other_fly}.")
        # t_up = pg.TextItem(f"Selected {self.other_fly}.", (255, 255, 255), anchor=(0, 0))
        # t_up.setPos(i + 0.5, -j + 0.5)
        # breakpoint()

    def synthetic_key(self, key):
        evt = QtGui.QKeyEvent(QtGui.QKeyEvent.KeyPress, key, QtCore.Qt.NoModifier) 
        self.app.sendEvent(self.cw, evt)
        self.app.processEvents()


def swap_flies(ds, index, swap_events, fly1=0, fly2=1):
    # use swap_dims to swap flies in all dataarrays in the data set?
    # TODO make sure this does not fail for datasets w/o song
    fs_song = ds.song.attrs['sampling_rate_Hz']
    fs_other = ds.pose_positions_allo.attrs['sampling_rate_Hz']
    index_other = int(index * fs_other / fs_song)
    if [index_other, fly1, fly2] in swap_events:  # if already in there remove - swapping a second time negates first swap
        swap_events.remove([index_other, fly1, fly2])
    else:
        swap_events.append([index_other, fly1, fly2])

    ds.pose_positions_allo.values[index_other:, [
        fly2, fly1], ...] = ds.pose_positions_allo.values[index_other:, [fly1, fly2], ...]
    ds.pose_positions.values[index_other:, [
        fly2, fly1], ...] = ds.pose_positions.values[index_other:, [fly1, fly2], ...]
    ds.body_positions.values[index_other:, [
        fly2, fly1], ...] = ds.body_positions.values[index_other:, [fly1, fly2], ...]
    return ds, swap_events


def save_swap_events(savefilename, lst):
    np.savetxt(savefilename, lst, fmt='%d', header='index fly1 fly2')


def main(datename: str = 'localhost-20181120_144618', root: str = '', cue_points: str = '[]'):
    """[summary]
    
    Args:
        datename (str): experiment id. Defaults to 'localhost-20181120_144618'.
        root (str): path containing the `dat` and `res` folders for the experiment. Defaults to ''.
        cue_points (str): Should evaluate to a list of indices. Defaults to '[]'.
    """
    # if os.path.exists(datename + '.zarr'):
    #     logging.info(f'Loading self.ds from {datename}.zarr.')
    #     self.ds = xb.load(datename + '.zarr')
    # else:
    logging.info(f'Assembling dataset for {datename}.')
    ds = xb.assemble(datename, root=root, fix_fly_indices=False)
    # logging.info(f'Saving self.ds to {datename}.zarr.')
    # xb.save(datename + '.zarr', self.ds)
    logging.info(ds)
    filepath = ds.attrs['video_filename']
    vr = VideoReaderNP(filepath[:-3] + 'avi')

    cue_points = eval(cue_points)

    psv = PSV(ds, vr, cue_points)

    # Start Qt event loop unless running in interactive mode or using pyside.
    if (sys.flags.interactive != 1) or not hasattr(QtCore, 'PYQT_VERSION'):
        QtGui.QApplication.instance().exec_()

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    defopt.run(main)