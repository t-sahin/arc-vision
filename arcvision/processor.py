import asyncio
import random
import sys
import cv2
import numpy as np
import time
from numpy import linalg
import matplotlib.pyplot as plt
# from utils import *
from .utils import *

import pickle
import scipy.stats as ss
from multiprocessing import Process, Pipe, Lock
import traceback

_object_id = 0
_sentinel = -1

def object_id():
    global _object_id
    _object_id += 1
    return _object_id

class Processor:
    '''A camera processor'''
    def __init__(self, camera, streams, stride, has_consumer=False, name=None):

        self.streams = streams
        if name is None:
            self.name = self.__class__.__name__
        else:
            self.name = name
        self.stride = stride
        camera.add_frame_processor(self)
        self.camera = camera

        #set-up offloaded thread and pipes for data
        self.has_consumer = has_consumer
        if has_consumer:
            self._work_conn, p = Pipe(duplex=True)
            self._lock = Lock()
            self.consumer = Process(target=self._consume_work, args=(p,self._lock))
            print('starting consumer thread....')
            self.consumer.start()



    @property
    def objects(self):
        return []

    def close(self):
        print('Closing ' + self.__class__.__name__)
        self.camera.remove_frame_processor(self)
        if self.has_consumer:
            self._work_conn.send(_sentinel)
            self.consumer.join()

    def _queue_work(self,data):
        asyncio.ensure_future(self._await_work(data))


    async def _await_work(self, data):
        # apparently you cannot await Connection objects???
        # also, there is some kind of buggy interaction when polling directlry
        # use a lock instead
        self._work_conn.send(data)
        while not self._lock.acquire(False):
            await asyncio.sleep(0) # do other things
        result = self._work_conn.recv()
        self._receive_result(result)
        self._lock.release()

    def _receive_result(self, result):
        '''override this to receive and process data which was porcessed via _process_work'''
        pass

    @classmethod
    def _consume_work(cls, return_conn, lock):
        '''This is the other thread main loop, which reads in data, handles the exit and calls _process_work'''
        while True:
            data = return_conn.recv()
            if data == _sentinel:
                break
            result = cls._process_work(data)
            lock.release()
            return_conn.send(result)
            lock.acquire()

    @classmethod
    def _process_work(cls, data):
        '''Override this method to process data passed to queue_work in a different thread'''
        pass


class ColorCalibrationProcessor(Processor):
    '''
    This will segment the frame and find the color mode in defined squares. This
    requires the spatial calibration to be done so that the coordinates can be
    transformed'''

    def __init__(self, camera, projector, grid_size=(8,8)):
        super().__init__(camera, ['camera-grid', 'camera-modes', 'projector-grid', 'projector-modes'] ,1)
        self.grid_size = grid_size
        self.projector = projector

    async def process_frame(self, frame, frame_ind):

        color_current = []
        for rect, color in self._iter_colors(frame):
            color_current.append(list(color))
        color_current_np = np.array(color_current, np.uint8).reshape(-1, 3)

        img = self.projector.frame

        color_target = []
        for rect, color in self._iter_colors(img):
            color_target.append(list(color))
        color_target_np = np.array(color_target, np.uint8).reshape(-1, 3)

        self._update_cap(color_target_np, color_current_np)

        return frame

    def _iter_rects(self, frame):
        dims = [frame.shape[i] // w for i,w in enumerate(self.grid_size)]
        #get 2D indices over grid size
        for index in np.indices( self.grid_size ).reshape(2, -1).T:
            start = index * dims
            yield (start[1], start[0], dims[1], dims[0])
        return frame

    def _iter_colors(self, frame):
        for r in self._iter_rects(frame):
           #get the mode for each channel and convert to int from length-1 array
            color = ( int(ss.mode(rect_view(frame[:,:,i], r).reshape(-1), axis=None)[0]) for i in range(frame.shape[2]) )
            color = tuple(color)
            yield r, color

    def _update_cap(self, color_target, color_current):
        '''Update the camera capture properties. Properties are defined in HSV space. color_target and color_current should
           be arrays of colors'''
        hsv_current = cv2.cvtColor(np.uint8(color_current).reshape(-1,1,3), cv2.COLOR_BGR2HSV)
        hsv_target = cv2.cvtColor(np.uint8(color_target).reshape(-1,1,3), cv2.COLOR_BGR2HSV)

        ratio = hsv_target.reshape(-1, 3) / hsv_current.reshape(-1, 3)
        avg_ratio = np.clip(np.mean(ratio, axis=0).reshape(3), 0.5, 2)

        print('Camera sees ')
        print(hsv_current)
        print('Projector sees ')
        print(hsv_target)
        print('Clipped ratio is ')
        print(avg_ratio)
        print('Camera moving from ', self.camera.cap.get(cv2.CAP_PROP_HUE), self.camera.cap.get(cv2.CAP_PROP_SATURATION), self.camera.cap.get(cv2.CAP_PROP_GAIN))
        self.camera.cap.set(cv2.CAP_PROP_HUE, round(avg_ratio[0] * max(1, self.camera.cap.get(cv2.CAP_PROP_HUE))))
        self.camera.cap.set(cv2.CAP_PROP_SATURATION, round(avg_ratio[1] * self.camera.cap.get(cv2.CAP_PROP_SATURATION)))
        self.camera.cap.set(cv2.CAP_PROP_GAIN, round(avg_ratio[2] * max(1, self.camera.cap.get(cv2.CAP_PROP_GAIN))))
        print('To ', self.camera.cap.get(cv2.CAP_PROP_HUE), self.camera.cap.get(cv2.CAP_PROP_SATURATION), self.camera.cap.get(cv2.CAP_PROP_GAIN))


    async def decorate_frame(self, frame, name):

        mode = None
        if name.split('-')[0] == 'projector':
            frame = self.projector.frame
            mode = name.split('-')[1]
        elif name.split('-')[0] == 'camera':
            mode = name.split('-')[1]



        for r,color in self._iter_colors(frame):
            if mode == 'modes':
                draw_rectangle(frame, r, color, -1)
            if mode is not None:
                draw_rectangle(frame, r, (0, 0, 0), 1)
                cv2.putText(frame,
                    '{}'.format(color),
                    (r[0] + 5, r[1] + r[3] // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255))
        return frame



class SpatialCalibrationProcessor(Processor):
    '''This will find a perspective transform that goes from our coordinate system
       to the projector coordinate system. Convergence in done by using point guess in next round with
       previous round estimate'''

    ''' Const for the serialization file name '''
    PICKLE_FILE = "spatialCalibrationData.pickle"

    def __init__(self, camera, background=None, channel=1, stride=1, N=16, delay=10, stay=20):
        #stay should be bigger than delay
        #stay is how long the calibration dot stays in one place (?)
        #delay is how long we wait before reading its position
        self.segmenter = SegmentProcessor(camera, background, -1, 4, max_rectangle=0.05, channel=channel, name='SpatialSegmenter')
        super().__init__(camera, ['calibration', 'transform'], stride)
        self.calibration_points = np.random.random( (N, 2)) * 0.8 + 0.1

        o = {}
        o['id'] = object_id()
        o['center_scaled'] = None
        o['label'] = 'calibration-point'
        self._objects = [o]

        self.index = 0
        self.delay = delay
        self.stay = stay
        self.N = N
        self.first = True
        self.channel = channel
        self.reset()

    @property
    def transform(self):
        return self._best_scaled_transform

    @property
    def inv_transform(self):
        return linalg.inv(self._best_scaled_transform)

    def close(self):

        super().close()
        self.segmenter.close()

    def play(self):
        self.calibrate = True

    def pause(self):
        self.calibrate = False

    def reset(self):
        # check if serialized data exists before loading the transform as the identity
        self.points = np.zeros( (self.N, 2) )
        self.counts = np.zeros( (self.N, 1) )
        self._transform = np.identity(3)
        self._scaled_transform = np.identity(3)
        self._best_scaled_transform = np.identity(3)
        self._best_fit = 0.01 #reasonable amount, anything less shouldn't be used
        self._best_list = np.array([1., 0., 0., 0., 1., 0., 0., 0. ,1.])
        self._best_inv_list = np.array([1., 0., 0., 0., 1., 0., 0., 0., 1.])
        self.fit = 100

        self.calibrate = False


    async def process_frame(self, frame, frame_ind):
        if self.calibrate:
            self._calibrate(frame, frame_ind)
        return frame

    def _calibrate(self, frame, frame_ind):
        if frame_ind % (self.stay + self.delay) > self.delay:
            for seg in self.segmenter.segments(frame):
                #if(rect_color_channel(frame, seg) == self.channel):
                p = rect_scaled_center(seg, frame)
                self.points[self.index, :] = self.points[self.index, :] * self.counts[self.index] / (self.counts[self.index] + 1) + p / (self.counts[self.index] + 1)
                self.counts[self.index] += 1
                break


        if frame_ind % (self.stay + self.delay) == 0:
            # update homography estimate
            if self.index == self.N - 1:
                print('updating homography')
                self._update_homography(frame)
                print(self._transform)
                self.calibration_points = np.random.random( (self.N, 2)) * 0.8 + 0.1
                #seed next round with fit, weighted by how well the homography fit
                self.points[:] = cv2.perspectiveTransform(self.calibration_points.reshape(-1,1,2), linalg.inv(self._transform)).reshape(-1,2)
                self.counts[:] = max(0, (0.01 - self.fit) * 10)
            self.index += 1
            self.index %= self.N

    def warp_img(self, img):
        return cv2.warpPerspective(img, self._best_scaled_transform, (img.shape[1], img.shape[0]))
        # for i in range(img.shape[2]):
        #     img[:,:,i] = cv2.warpPerspective(img[:,:,i],
        #                                     self._best_scaled_transform,
        #                                     img.shape[1::-1])
        return img

    def warp_point(self, point):
        w = point[0]* self._best_list[6] + point[1] * self._best_list[7] + self._best_list[8]
        x = point[0] * self._best_list[0] + point[1] * self._best_list[1] + self._best_list[2]
        y = point[0] * self._best_list[3] + point[1] * self._best_list[4] + self._best_list[5]
        if (w != 0):
            point[0] = x/w
            point[1] = y/w
        else:
            point[0] = 0
            point[1] = 0
        return point

    def unwarp_point(self, point):
        w = point[0]* self._best_inv_list[6] + point[1] * self._best_inv_list[7] + self._best_inv_list[8]

        x = point[0] * self._best_inv_list[0] + point[1] * self._best_inv_list[1] + self._best_inv_list[2]
        y = point[0] * self._best_inv_list[3] + point[1] * self._best_inv_list[4] + self._best_inv_list[5]
        if (w != 0):
            point[0] = x/w
            point[1] = y/w
        else:
            point[0] = 0
            point[1] = 0
        return point

    def _update_homography(self, frame):
        if(np.sum(self.counts > 0) < 5):
            return
        t, mask = cv2.findHomography((self.points[self.counts[:,0] > 0, :]).reshape(-1, 1, 2),
                                self.calibration_points[self.counts[:,0] > 0, :].reshape(-1, 1, 2),
                                0)
        print('---------------')
        p = cv2.perspectiveTransform((self.points).reshape(-1, 1, 2), self._transform).reshape(-1, 2)
        for i in range(self.N):
            #print('Points  {} (used:{}): ({}) ({}) ({})'.format(i, mask[i], self.points[i,:], p[i, :], self.calibration_points[i,:]))
            print('Points  {}: ({}) ({}) ({})'.format(i, self.points[i,:], p[i, :], self.calibration_points[i,:]))
        print('---------------')
        ts, _ = cv2.findHomography(self._unscale(self.points[self.counts[:,0] > 0, :], frame.shape).reshape(-1, 1, 2),
                    self._unscale(self.calibration_points[self.counts[:,0] > 0,:], frame.shape).reshape(-1, 1, 2),
                    0)
        if t is None:
            print('homography failed')
        else:
            if(self.first):
                self._transform = t
                self._scaled_transform = ts
                self.first = False
            else:
                self._transform = self._transform * 0.6 + t * 0.4
                self._scaled_transform = self._scaled_transform * 0.6 + ts * 0.4

        # get fit relative to identity
        self.fit = linalg.norm(self.calibration_points.reshape(-1, 1, 2) - cv2.perspectiveTransform((self.points).reshape(-1, 1, 2), self._transform)) / self.N
        if self.fit < self._best_fit:
            self._best_scaled_transform = self._scaled_transform
            self._best_fit = self.fit
            self._best_list = (self._transform).flatten()
            self._best_inv_list = (linalg.inv(self._transform)).flatten()
            print(self._best_list)

    def _unscale(self, array, shape):
        return (array * [shape[1], shape[0]]).astype(np.int32)

    async def decorate_frame(self, frame, name):
        if name == 'transform':
            self.warp_img(frame)
        if name == 'calibration' or name == 'transform':
            for i in range(self.N):
                p = np.copy(self.calibration_points[i, :])

                c = self.unwarp_point(p)
                c = self._unscale(c, frame.shape)

                #BGR

                # points represents the found location of the calibration circle (printed in red)
                cv2.circle(frame,
                            tuple(self._unscale(self.points[i],
                                frame.shape)), 10, (0,0,255), -1)

                # c is the ground-truth location of the calibration circle, unwarped to be in CV space
                # printed in blue. these should be close to the red points if the fit is good
                cv2.circle(frame,
                            tuple(c.astype(np.int)), 10, (255,0,0), -1)

                # calibration points
                cv2.circle(frame,
                            tuple(self._unscale(self.calibration_points[i, :],
                                frame.shape)), 10, (0,255, 255), -1)
                # draw a purple line between the corresponding red and blue dot to track distance
                cv2.line(frame, tuple(self._unscale(self.points[i],
                                frame.shape)), tuple(c.astype(np.int)), (255,0,255))
            # draw rectangle of the transform
            p_rect = np.array( [[0, 0], [0, 1], [1, 1], [1, 0]], np.float).reshape(-1, 1, 2)
            c_rect = cv2.perspectiveTransform(p_rect, (self._transform))
            c_rect = self._unscale(c_rect, frame.shape)
            cv2.polylines(frame, [c_rect], True, (0, 255, 125), 4)
            cv2.putText(frame,
                'Homography Fit: {}'.format(self.fit),
                (100, 250),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,124,255))
        return frame

    @property
    def objects(self):
        if self.calibrate:
            self._objects[0]['center_scaled'] = self.calibration_points[self.index]
            return self._objects
        return []

class CropProcessor(Processor):
    '''Class that detects multiple objects given a set of labels and images'''
    def __init__(self, camera, rect=None, stride=1):
        super().__init__(camera, ['crop'], stride)
        self.rect = rect

    async def process_frame(self, frame, frame_ind):
        '''Perform update on frame, carrying out algorithm'''
        if self.rect is not None:
            return rect_view(frame, self.rect)
        return frame

    async def decorate_frame(self, frame, name):
        '''Draw visuals onto the given frame, without carrying-out update'''
        return await self.process_frame(frame, 0)

class PreprocessProcessor(Processor):
    '''Substracts and computes background'''
    def __init__(self, camera, stride=1, gamma=2.5):
        super().__init__(camera, ['preprocess'], stride)
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))

    async def process_frame(self, frame, frame_ind):
        '''Perform update on frame, carrying out algorithm'''
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        equ = self.clahe.apply(frame)
        #return equ[:,:, np.newaxis]
        return cv2.cvtColor(equ, cv2.COLOR_GRAY2BGR)

    async def decorate_frame(self, frame, name):
        # go to BW but don't remove channel
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        equ = self.clahe.apply(frame)
        equ = cv2.cvtColor(equ, cv2.COLOR_GRAY2BGR)
        return equ


class BackgroundProcessor(Processor):
    '''Substracts and computes background'''
    def __init__(self, camera):
        super().__init__(camera, ['background-removal'], 1)
        self.reset()
        self.pause()
        self._background = None
        self._blank = None

    @property
    def background(self):
        return self._background

    def pause(self):
        self.paused = True
    def play(self):
        self.paused = False
    def reset(self):
        self.count = 0
        self.avg_background = None
        self.paused = False

    async def process_frame(self, frame, frame_ind):
        '''Perform update on frame, carrying out algorithm'''

        if self.avg_background is None:
            self.avg_background = np.empty(frame.shape, dtype=np.uint32)
            self.avg_background[:] = 0
            self._background = frame.copy()
            self._blank = np.zeros((frame.shape))

        if not self.paused:
            self.avg_background += frame
            self.count += 1
            self._background = self.avg_background // max(1, self.count)
            self._background = self._background.astype(np.uint8)
            self._background = cv2.blur(self._background, (5,5))
        #do max to protect against underflow
        #return np.maximum(frame - self._background, self._blank)
        return frame


    async def decorate_frame(self, frame, name):
        #return np.maximum(frame - self._background, self._blank)
        return frame

class TrackerProcessor(Processor):

    @property
    def objects(self):
        '''Objects should have a dictionary with center, bbox, name, and id'''
        return self._tracking


    def __init__(self, camera, detector_stride, delete_threshold_period=2.5, stride=1):
        super().__init__(camera, ['track'], stride)
        self._tracking = []
        self.labels = {}
        # need to keep our own ticks because
        # we don't know frame index when track() is called
        self.ticks = 0
        if detector_stride > 0:
            self.ticks_per_obs = detector_stride * delete_threshold_period / self.stride

    async def process_frame(self, frame, frame_ind):
        self.ticks += 1
        delete = []
        for i,t in enumerate(self._tracking):
            start = time.time()
            status,bbox = t['tracker'].update(frame)
            end = time.time()
            #print('Updated bbox is {}, elapse time is {}'.format(bbox, end-start))
            t['observed'] -= 1
            # we know our objects should stay the same size all of the time.
            # check if the size dramatically changed.  if so, the object most likely was removed
            # if not, rescale the tracked bbox to the correct size
            if(status):
                areaDiff = abs(rect_area(bbox) - t['area_init'])/t['area_init']
                # experimental value: if the size has changed by more than 15%, count that as a failed observation

                # check if its new location is a reflection, or drastically far away
                if (areaDiff <= -.15):
                    t['observed'] -= 1
                    #print("Area difference is {}, counting as null observation".format(areaDiff))
                else:
                    # rescale the bbox to match the original area?
                    scaleFactor = t['area_init']/rect_area(bbox)
                    bbox_p = stretch_rectangle(bbox, frame, scaleFactor)
                    t['delta'][0] = bbox[0] - t['init'][0]
                    t['delta'][1] = bbox[1] - t['init'][1]
                    t['bbox'] = bbox_p
                    t['center_scaled'] = rect_scaled_center(bbox_p, frame)


            # check obs counts
            if t['observed'] < 0:
                delete.append(i)
        offset = 0
        delete.sort()
        for i in delete:
            del self._tracking[i - offset]
            offset += 1

        return frame

    def _unscale(self, array, shape):
        return (array * [shape[1], shape[0]]).astype(np.int32)

    async def decorate_frame(self, frame, name):
        if name != 'track':
            return frame
        for i,t in enumerate(self._tracking):
            if(t['observed'] < 3):
                continue
            bbox = t['bbox']
            draw_rectangle(frame, bbox, (0,0,255), 1)
            center = t['center_scaled']
            center_pos = np.array(center)
            cv2.circle(frame,tuple(self._unscale(center_pos,frame.shape)), 10, (0,0, 255), -1)
            #now draw polygon
            cv2.polylines(frame,[t['poly'] + t['delta']], True, (0,0,255), 3)

            #put note about it
            cv2.putText(frame,
                        '{}: {}'.format(t['name'], t['observed']),
                        (0, 60 * (i+ 1)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255))

        return frame

    def track(self, frame, bbox, poly, label):
        '''

        '''
        if label in self.labels:
            self.labels[label] += 1
        else:
            self.labels[label] = 1

        #we need to make sure we don't have an existing object here
        for t in self._tracking:
            intersection = intersecting(bbox, t['bbox'])
            if intersection is not None and intersection > 0.25:
                if label != t['label']:
                    #reclassification
                    self.labels[label] -= 1
                    t['name'] = '{}-{}'.format(label, self.labels[label] - 1)
                    t['label'] = label
                # found existing one
                # add to count
                t['observed'] = self.ticks_per_obs
                #update polygon and bounding box and very different
                if intersection < 0.55:
                    # reduce the size of the bbox by 5% so it has less of a chance of including the reactor image in its initial polygon
                    scaled_center = rect_scaled_center(bbox,frame)
                    bbox_p = stretch_rectangle(bbox, frame, .95)
                    t['center_scaled'] = scaled_center
                    t['poly'] = poly
                    t['init'] = bbox
                    t['delta'] = np.int32([0,0])
                    t['area_init'] = rect_area(bbox)
                    t['tracker'] = cv2.TrackerMedianFlow_create()
                    t['tracker'].init(frame, bbox)
                return


        name = '{}-{}'.format(label, self.labels[label] - 1)

        tracker = cv2.TrackerMedianFlow_create()
        status = tracker.init(frame, bbox)

        if not status:
            print('Failed to initialize tracker')
            return False

        track_obj = {'name': name,
                     'tracker': tracker,
                     'label': label,
                     'poly': poly,
                     'init': bbox,
                     'area_init':rect_area(bbox),
                     'center_scaled': rect_scaled_center(bbox, frame),
                     'bbox': bbox,
                     'observed': self.ticks_per_obs,
                     'start': self.ticks,
                     'delta': np.int32([0,0]),
                     'id': object_id()}
        self._tracking.append(track_obj)
        return True

class SegmentProcessor(Processor):
    def __init__(self, camera, background, stride, max_segments, max_rectangle=0.25, channel=None, hsv_delta=[100, 100, 16], name=None):
        '''Pass stride = -1 to only process on request'''
        super().__init__(camera, [

                                  'background-subtract',
                                  'background-filter-blur',
                                  'background-thresh',
                                  'background-erode',
                                  'background-open',
                                  'distance',
                                  'boxes',
                                  'watershed'
                                  ], max(1, stride), name=name)
        self.rect_iter = range(0)
        self.background = background
        self.max_segments = max_segments
        self.max_rectangle = max_rectangle
        self.own_process = (stride != -1)
        self.channel = channel

        if channel is not None:
            # convert channel specification to an HSV value
            color = [0, 0, 0]
            color[channel] = 255
            hsv = cv2.cvtColor(np.uint8(color).reshape(-1,1,3), cv2.COLOR_BGR2HSV).reshape(3)
            # create an interval from that
            h_min = max(0, hsv[0] - hsv_delta[0])
            h_max = min(255, hsv[0] + hsv_delta[0])
            #swap them in case of roll-over
            #h_min, h_max = min(h_min, h_max), max(h_min,h_max)
            self.hsv_min = np.array([h_min, max(0,hsv[1] - hsv_delta[1]), hsv_delta[2]], np.uint8)
            self.hsv_max = np.array([h_max, 255, 255], np.uint8)

            print('range of color hsv', self.hsv_min, hsv, self.hsv_max)


    async def process_frame(self, frame, frame_ind):
        '''we only process on request'''
        if self.own_process:
            return self._process_frame(frame, frame_ind)
        return frame

    def _process_frame(self, frame, frame_ind):
        bg = self._filter_background(frame)
        dist_transform = self._filter_distance(bg)
        self.rect_iter = self._filter_contours(dist_transform)
        return frame

    def segments(self, frame = None):
        if frame is not None:
            self._process_frame(frame, 0)
        yield from self.rect_iter


    def _filter_background(self, frame, name = ''):

        img = frame.copy()
        #if(frame.shape == self.background.shape):
        #    img -= self.background

        if name == 'background-subtract':
            return img
        gray = img
        if self.channel is None:
            if len(img.shape) == 3 and img.shape[2] == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = cv2.inRange(img, self.hsv_min, self.hsv_max)
        gray = cv2.blur(gray, (3,3))
        if name == 'background-filter-blur':
            return gray
        ret, bg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        #bg = cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        #                                cv2.THRESH_BINARY_INV,11,2)
        if np.mean(bg) > 255 // 2:
           bg = 255 - bg

        if name == 'background-thresh':
            return bg
        # noise removal
        kernel = np.ones((4,4),np.uint8)
        bg = cv2.erode(bg, kernel, iterations = 2)
        if name == 'background-erode':
            return bg
        bg = cv2.morphologyEx(bg,cv2.MORPH_OPEN,kernel, iterations = 1)
        if name == 'background-open':
            return bg

        return bg

    def _filter_distance(self, frame):
        dist_transform = cv2.distanceTransform(frame, cv2.DIST_L2,5)
        dist_transform = cv2.normalize(dist_transform, dist_transform, 0, 255, cv2.NORM_MINMAX)

        #create distance tranform contours
        dist_transform = np.uint8(dist_transform)
        return dist_transform

    def _filter_ws_markers(self, frame):
        _, contours, _ = cv2.findContours(frame, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        #create markers
        markers = np.zeros( frame.shape, dtype=np.uint8 )

        for i in range(len(contours)):
            #we draw onto our markers with fill to create the mask
            cv2.drawContours(markers, contours, i, (i + 1,), -1)
        #draw a tiny circle to indicate background hint
        cv2.circle(markers, (5,5), 3, (255,))
        return markers.astype(np.int32)

    def _filter_contours(self, frame, return_contour=False):
        _, contours, _ = cv2.findContours(frame, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        def sort_key(c):
            '''The area of bounding rectangle'''
            rect = cv2.boundingRect(c)
            return rect[2] * rect[3]
        contours.sort(key = sort_key, reverse=True)
        rects = [cv2.boundingRect(c) for c in contours]
        segments = 0

        for c,r in zip(contours, rects):
            #flip around our rectangle
            # exempt small or large rectangles
            if(r[2] * r[3] < 250 or \
                r[2] * r[3] / frame.shape[0] / frame.shape[1] > self.max_rectangle ):
                continue
            if not return_contour:
                yield r
            else:
                yield c
            segments += 1
            if(segments == self.max_segments):
                break


    def _watershed(self, frame, markers):
        ws_markers = cv2.watershed(frame, markers)
        segments = 0
        for i in range(1, np.max(ws_markers)):
            pixels = np.argwhere(ws_markers == i)
            rect = cv2.boundingRect(pixels)
            #flip around our rectangle
            rect = (rect[1], rect[0], rect[3], rect[2])
            # exempt small or large rectangles (> 25 % of screen)
            if(len(pixels) < 5 or rect[2] * rect[3] < 100 or \
                rect[2] * rect[3] / frame.shape[0] / frame.shape[1] > self.max_rectangle ):
                continue
            yield rect
            segments += 1
            if(segments == self.max_segments):
                break

    def polygon(self, frame, rect = None):
        '''
            rect: an optional view which will limit the frame
        '''
        bg = self._filter_background(frame)
        dist_transform = self._filter_distance(bg)
        # filter herre
        if rect is not None:
            dist_transform = rect_view(dist_transform, rect)
            frame = rect_view(frame, rect)

        markers = self._filter_ws_markers(dist_transform)
        ws_markers = cv2.watershed(frame, markers)

        #sort based on size
        pixels = [np.flip(np.argwhere(ws_markers == i), axis=1) for i in range(1, np.max(ws_markers))]
        def key(x):
            r = cv2.boundingRect(x)
            return r[2] * r[3]
        pixels.sort(key = key, reverse=True)
        # add a polygon of the whole rect first
        result = []
        segments = 0
        for p in pixels:
            # exempt small rectangles
            rect = cv2.boundingRect(p)
            if(len(p) < 5 or rect[2] * rect[3] < 20 ):
                continue
            # once we find one, use it
            hull = cv2.convexHull(p)
            result.append((hull, rect))

            segments += 1
            if segments > self.max_segments:
                break

        #This code doesn't seem to fill the rectangle and I cannot figure out why
        result.append(np.array([
                     [0,0],
                    [0,frame.shape[0]],
                    [frame.shape[1], frame.shape[0]],
                    [frame.shape[1], 0],
                    ], np.int32).reshape(-1, 1, 2))
        return result

    async def decorate_frame(self, frame, name):
        bg = self._filter_background(frame, name)
        if name.find('background') != -1:
            return bg

        dist_transform = self._filter_distance(bg)
        if name == 'distance':
            return dist_transform

        if name == 'boxes':
            for rect in self._filter_contours(dist_transform):
                draw_rectangle(frame, rect, (255, 255, 0), 1)
        if name == 'watershed':
            markers = self._filter_ws_markers(dist_transform)
            ws_markers = cv2.watershed(frame, markers)
            frame[ws_markers == -1] = (255, 0, 0)
        return frame


class TrainingProcessor(Processor):

    @property
    def objects(self):
        '''Objects should have a dictionary with center, bbbox, name, and id'''
        return self._objects

    ''' This will segment an ROI from the frame and you can label it'''
    def __init__(self, camera, img_db, descriptor, background = None, max_segments=3):
        super().__init__(camera, ['training'], 1)
        self.segmenter = SegmentProcessor(camera, background, -1, max_segments)
        self.img_db = img_db
        self.rect_index = 0
        self.poly_index = 0
        self.poly_len = 0
        self.rect_len = 0
        self.rect = (0,0,100,100)
        self.segments = []
        self.polys = []
        self.poly = np.array([[0,0], [0,0]])
        self.descriptor = descriptor
        self._objects = []

    def close(self):
        super().close()
        self.segmenter.close()

    def set_descriptor(self, desc):
        self.descriptor = desc

    async def process_frame(self, frame, frame_ind):
        self.segments = list(self.segmenter.segments(frame))
        self.rect_len = len(self.segments)
        if self.rect_index >= 0 and self.rect_index < len(self.segments):
            self.rect = stretch_rectangle(self.segments[self.rect_index], frame)

        # index 1 is poly
        self.polys = [x[0] for x in self.segmenter.polygon(frame, self.rect)]
        self.poly_len = len(self.polys)
        if self.poly_index >= 0 and self.poly_index < len(self.polys):
            self.poly = self.polys[self.poly_index]

        return frame

    async def decorate_frame(self, frame, name):

        if name == 'training':


            kp, _ = keypoints_view(self.descriptor, frame, self.rect)
            cv2.drawKeypoints(frame, kp, frame, color=(32,32,32), flags=0)

            for r in self.segments:
                draw_rectangle(frame, r, (60, 60, 60), 1)
            draw_rectangle(frame, self.rect, (255, 255, 0), 3)

            frame_view = rect_view(frame, self.rect)
            for p in self.polys:
                cv2.polylines(frame_view, [p], True, (60, 60, 60), 1)
            cv2.polylines(frame_view, [self.poly], True, (0, 0, 255), 3)

        return frame

    def capture(self, frame, label):
        '''Capture and store the current image'''
        img = rect_view(frame, self.rect)
        # process it
        kp = self.descriptor.detect(img, None)
        if(len(kp) < 4):
            return False
        processed = img.copy()
        cv2.drawKeypoints(processed, kp, processed, color=(32,32,32), flags=0)
        cv2.polylines(processed, [self.poly], True, (0,0,255), 3)
        self.img_db.store_img(img, label, self.poly, kp, processed)

        #create obj
        self._objects = [{
            'bbox': self.rect,
            'center_scaled': rect_scaled_center(self.rect, frame),
            'label': label,
            'id': object_id()
        }]

        return True


class DetectionProcessor(Processor):
    '''Detects query images in frame. Uses async to spread out computation. Cannot handle replicas of an object in frame'''
    def __init__(self, camera, background, img_db, descriptor, stride=5,
                 threshold=0.8, template_size=256, min_match=6,
                 weights=[3, -1, -1, -10, 5], max_segments=10,
                 track=True):

        #we have a specific order required
        #set-up our tracker
        # give estimate of our stride
        if track:
            self.tracker = TrackerProcessor(camera, stride * 2 * len(img_db))
        else:
            self.tracker = None
        #then our segmenter
        self.segmenter = SegmentProcessor(camera, background, -1, max_segments)
        #then us
        super().__init__(camera, ['keypoints', 'identify'], stride)


        self._ready = True
        self.features = {}
        self.threshold = threshold
        self.min_match = min_match
        self.weights = weights
        self.stretch_boxes=1.5
        self.track = track
        self.templates = img_db


        # Initiate descriptors
        self.desc = descriptor
        #set-up our matcher
        self.matcher = cv2.BFMatcher()

        #create color gradient
        N = len(img_db)
        cm = plt.cm.get_cmap('Dark2')
        for i,t in enumerate(self.templates):
            rgba = cm(i / N)
            rgba = [int(x * 255) for x in rgba]
            t.color = rgba[:-1]
            if t.keypoints is None:
                t.keypoints = self.desc.detect(t.img)
            t.keypoints, t.features = self.desc.compute(t.img, t.keypoints)

    @property
    def objects(self):
        if self.tracker is None:
            return []

        return self.tracker.objects

    def close(self):
        super().close()
        self.segmenter.close()
        self.tracker.close()

    def set_descriptor(self, desc):
        self.desc = desc
        for i, t in enumerate(self.templates):
            t.keypoints, t.features = self.desc.detectAndCompute(t.img, None)

    async def process_frame(self, frame, frame_ind):
        if(self._ready):
            #copy the frame into it so we don't have it processed by later methods
            asyncio.ensure_future(self._identify_features(frame.copy()))
        return frame

    async def decorate_frame(self, frame, name):

        if name != 'keypoints' and name != 'identify':
            return frame

        # draw key points
        for rect in self.segmenter.segments(frame):
            kp,_ = keypoints_view(self.desc, frame, rect)
            if(kp is not None):
                cv2.drawKeypoints(frame, kp, frame, color=(32,32,32), flags=0)
            # draw the rectangle that we use for kp
            rect = stretch_rectangle(rect, frame)
            draw_rectangle(frame, rect, (255, 0, 0), 1)

        if name == 'keypoints':
            return frame
        for n in self.features:
            for f in self.features[n]:
                points = f['poly']
                color = f['color']
                kp = f['kp']
                kpcolor = f['kpcolor']
                for p,c in zip(kp, kpcolor):
                    cv2.circle(frame, tuple(p), 6, color, thickness=-1)

                #draw polygon
                cv2.polylines(frame,[points],True, color, 3, cv2.LINE_AA)
                #get bottom of polygon
                cv2.putText(frame, '{} ({:.2})'.format(n, f['score']),
                            (f['rect'][0], f['rect'][1]),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, color)

        return frame

    async def _identify_features(self, frame):
        self._ready = False
        #make new features object
        features = {}

        found_feature = False
        for rect in self.segmenter.segments(frame):
            kp, des = keypoints_view(self.desc, frame, rect)
            if(des is not None and len(des) > 3):
                rect_features = await self._process_frame_view(frame, kp, des, rect)
                if len(rect_features) > 0:
                    found_feature = True
                    best = max(rect_features, key=lambda x: rect_features[x]['score'])
                    if best in features:
                        features[best].append(rect_features[best])
                    else:
                        features[best] = [rect_features[best]]
        if found_feature:
            self.features = features
        self._ready = True

    async def _process_frame_view(self, frame, kp, des, bounds):
        '''This method tries to run the calculation over multiple loops.
            The _ready is to in lieu of a callback on completion'''
        self._ready = False
        features = {}
        for t in self.templates:
            try:
                template = t.img
                name = t.label
                descriptors = t.keypoints, t.features


                matches = self.matcher.knnMatch(descriptors[1], des, k=2)
                # store all the good matches as per Lowe's ratio test.
                good = []
                if(len(matches) > 1): #not sure how this happens
                    for m,n in matches:
                        if m.distance < self.threshold * n.distance:
                            good.append(m)
                # check if we have enough good points
                if len(good) > self.min_match:

                    # look-up actual x,y keypoints
                    src_pts = np.float32([ descriptors[0][m.queryIdx].pt for m in good ]).reshape(-1,1,2)
                    dst_pts = np.float32([ kp[m.trainIdx].pt for m in good ]).reshape(-1,1,2)

                    # use homography to find matrix transform between them
                    M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC,3.0)
                    if M is None:
                        continue

                    src_poly = np.float32(t.poly).reshape(-1,1,2)
                    dst_poly = cv2.perspectiveTransform(src_poly,M)
                    dst_bbox = cv2.boundingRect(dst_poly)

                    # check if the polygon is actually good
                    area = max(0.01, cv2.contourArea(dst_poly))
                    perimter = max(0.01, cv2.arcLength(dst_poly, True))
                    score = len(good) / len(des) * self.weights[0] + \
                            perimter / area * self.weights[1] + \
                            (dst_bbox[2] / bounds[2] - 1 + dst_bbox[3] /  bounds[3] - 1) * self.weights[2] + \
                            (dst_bbox[2] * dst_bbox[3] < 5) * self.weights[3] + \
                            self.weights[4]
                    if score > 0:
                        cm = plt.cm.get_cmap()
                        features[name] = { 'color': t.color, 'poly': np.int32(dst_poly),
                            'kp': np.int32([kp[m.trainIdx].pt for m in good]).reshape(-1,2),
                            'kpcolor': [cm(x.distance / good[-1].distance) for x in good],
                            'score': score, 'rect': bounds}
                        # register it with our tracker
                        if self.track:
                            self.tracker.track(frame, bounds, np.int32(dst_poly), name)
            except cv2.error:
                #not enough points
                await asyncio.sleep(0)
                continue

            #cede control
            await asyncio.sleep(0)
        return features

class LineDetectionProcessor(Processor):
    ''' Detects drawn lines on an image (NB: works with a red marker or paper strip)
        This will not return knowledge of connections between reactors (that logic should be in DetectionProcessor, which this class should be controlled by)
    '''
    def __init__(self, camera, stride, background):
        super().__init__(camera, ['image-segmented','lines-detected'])
        _lines = [] # initialize as an empty array - list of endpoints
        _background = cv2.bilateralFilter(background, 7, 150, 150) # what should we do about eventual raster noise?

    async def process_frame(self, frame, frame_ind):
        if(self._ready):
            #copy the frame into it so we don't have it processed by later methods
            asyncio.ensure_future(self._detect_lines(frame.copy()))
        return frame

    async def decorate_frame(self, frame, name):

        if name != 'image-segmented' or name != 'lines-detected':
            return frame

        if name == 'image-segmented':
            return self.threshold_background(frame)

        if name == 'lines-detected':
            lines = self._detect_lines(frame)
            # add purple points for each endpoint detected
            for i in range(0,len(lines)):
                cv2.circle(frame, (lines[i][0][0], lines[i][0][1]), (255,0,255),-1)
                cv2.circle(frame, (lines[i][1][0], lines[i][1][1]), (255,0,255),-1)

        return frame

    '''
    Use _detect_lines to get currently found lines, and compare to the previously found ones.  Adjust/add/remove from _lines property
    '''
    def detect_lines(self,frame):
        new_lines = self.detect_lines(frame)
        pass

    ''' Detect lines using filtered contour detection on the output of threshold_background
    '''
    def _detect_lines(self,frame):
        mask = self.threshold_background(frame)
        lines = []
        # detect contours on this mask
        _, contours, _ = cv2.findContours(mask, 1,cv2.CHAIN_APPROX_SIMPLE)
        if (contours is not None):
            for i in range(0, len(contours)):
                rect = cv2.minAreaRect(contours[i])
                # rect is a Box2D struct containing (x,y) as the center of the box, (w,h) as the width and height, and theta as the rotation
                area = rect[1][0]*rect[1][1]
                minDim = min(rect[1]) # the thickness of the line - we want to throw out rectangles that are too big
                maxDim = max(rect[1]) #corresponds to length - we want to throw out any noisy points that are too small
                if (rect[1][0] != 0 and rect[1][1] != 0):
                    aspectRatio = float(min(rect[1]))/max(rect[1])
                else:
                    aspectRatio = 100

                # we want a thin object, so a small aspect ratio.
                aspect_ratio_thresh = 0.3
                area_thresh_upper = 20000
                area_thresh_lower = 200
                width_thresh = 25
                length_thresh = 30
                if (aspectRatio < aspect_ratio_thresh and area > area_thresh_lower and area < area_thresh_upper and minDim < width_thresh and maxDim > length_thresh):
                    # only do vertex calculation if it is the correct shape
                    endpoints = box_to_endpoints(rect)
                    transpose_shape = np.flip(np.array(mask.shape),0)
                    ep1_scaled = endpoints[0]/transpose_shape
                    ep2_scaled = endpoints[1]/transpose_shape

                    # only accept lines that are within the 10%-90% bounds of the frame
                    ep1_within_bound = (min(ep1_scaled) > .1 and max(ep1_scaled) < .9)
                    ep2_within_bound = (min(ep2_scaled) > .1 and max(ep2_scaled) < .9)
                    if (ep1_within_bound and ep2_within_bound):
                        lines.append()

        return lines

    '''
        Run absDiff to subtract the background from the frame, then normalize over each color channel
    '''
    def absDiff_background(self,frame):
        image_diff = cv2.absdiff(self._background, frame)
        sum_diff = np.sum(image_diff, 2).astype(np.uint8)
        sum_diff = cv2.medianBlur(sum_diff, 9)
        return sum_diff

    '''
    Call absDiff_background, use a binary threshold to create a mask
    '''
    def threshold_background(self,frame):
        sum_diff = self.absDiff_background(frame)
        # threshold this value- play with thresh_val in prod
        thresh_val = 20
        _,mask = cv2.threshold(sum_diff, thresh_val, 255, cv2.THRESH_BINARY)
        # apply a sharpening filter
        kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
        mask = cv2.filter2D(mask,-1,kernel)
        return mask

    '''
    Lazy implementation of finding endpoints of a bounding rectangle.  Find the vertices, use the lowest and its hypoteneuse
    '''
    def box_to_endpoints(rect):
        box = np.int0(cv2.boxPoints(rect))
        return (box[0],box[2])
