import asyncio, sys, cv2, os, time, pickle, traceback, pathlib
import numpy as np
from numpy import linalg
from .utils import *
from multiprocessing import Process, Pipe, Lock

SOURCE_ID = 0
CONDITIONS_ID = 999
OBJECT_ID = 1 # 0 and 999 are reserved for temperature
SENTINEL = -1

def object_id():
    global OBJECT_ID
    OBJECT_ID += 1
    return OBJECT_ID

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
            self._work_conn.send(SENTINEL)
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
        '''override this to receive and process data which was processed via _process_work'''
        pass

    @classmethod
    def _consume_work(cls, return_conn, lock):
        '''This is the other thread main loop, which reads in data, handles the exit and calls _process_work'''
        while True:
            data = return_conn.recv()
            if data == SENTINEL:
                break
            result = cls._process_work(data)
            lock.release()
            return_conn.send(result)
            lock.acquire()

    @classmethod
    def _process_work(cls, data):
        '''Override this method to process data passed to queue_work in a different thread'''
        pass


class SpatialCalibrationProcessor(Processor):
    '''This will find a perspective transform that goes from our coordinate system
       to the projector coordinate system. Convergence in done by using point guess in next round with
       previous round estimate'''

    ''' Const for the serialization file name '''
    PICKLE_FILE = pathlib.Path('.') / 'calibrationdata' / 'spatialCalibrationData.p'

    def __init__(self, camera, background=None, channel=1, stride=1, N=16, delay=10, stay=20, readAtInit = True, segmenter = None):
        #stay should be bigger than delay
        #stay is how long the calibration dot stays in one place (?)
        #delay is how long we wait before reading its position
        if segmenter is None:
            self.segmenter = SegmentProcessor(camera, background, -1, 4, max_rectangle=0.25, channel=channel, name='Spatial')
        else:
            self.segmenter = segmenter
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
        self.readAtReset = readAtInit
        self.frameWidth = camera.cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        self.frameHeight = camera.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        self.reset()


    @property
    def background(self):
        return None

    @background.setter
    def background(self, background):
        self.segmenter.background = background


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

    def _read_calibration(self, filepath):
        if(os.path.exists(filepath)):
            # check if the currently read file has an entry for this resolution
            allData = pickle.load(open(filepath, 'rb'))
            res_string = '{}x{}'.format(self.frameWidth, self.frameHeight)
            if (res_string in allData):
                print(f'Reading homography from {filepath}')
                data = allData[res_string]
                self.first = False
                self._transform = data['transform']
                self._scaled_transform = data['scaled_transform']
                self._best_scaled_transform = data['best_scaled_transform']
                self._best_list = data['best_list']
                self._best_inv_list = data['best_inv_list']
                self.fit = data['fit']
                self._best_fit = 0.01
                self.calibrate = False
                self.first = False
                self.initial_fit = self.fit
                return True
        return False

    def _write_calibration(self, filepath):
        if (os.path.exists(filepath)):
                # read the existing data file to update
                data = pickle.load(open(filepath, 'rb'))
        if (os.path.exists(filepath)):
                  # read the existing data file to update
            data = pickle.load(open(filepath, 'rb'))
        else:
            # start fresh
            data = {}
        # create a sub-dict for this resolution
        subData = {}
        subData['transform'] = self._transform
        subData['scaled_transform']=self._scaled_transform
        subData['best_scaled_transform'] = self._best_scaled_transform
        subData['best_list'] = self._best_list
        subData['best_inv_list'] = self._best_inv_list
        subData['fit'] = self.fit
        subData['width'] = self.frameWidth
        subData['height'] = self.frameHeight

        # add it to the existing dictionary, then write the updated data out
        data['{}x{}'.format(self.frameWidth, self.frameHeight)] = subData
        print(f'Writing calibration to {filepath}')
        #make sure directory is ready
        try:
            os.makedirs(filepath.parent)
        except FileExistsError:
            #OK if it exists
            pass
        pickle.dump(data, open(filepath, 'wb'))

    def pause(self):
        # only write good fits/better than the previously calculated one
        if (self.fit < .001 and self.fit < self.initial_fit):
            self._write_calibration(SpatialCalibrationProcessor.PICKLE_FILE)

        self.calibrate = False



    def reset(self):
        self.points = np.zeros( (self.N, 2) )
        self.counts = np.zeros( (self.N, 1) )
        #try to read the pickle file
        if not self.readAtReset or not self._read_calibration(SpatialCalibrationProcessor.PICKLE_FILE):
            #didn't work, set to defaults
            self._transform = np.identity(3)
            self._scaled_transform = np.identity(3)
            self._best_scaled_transform = np.identity(3)
            self._best_fit = 0.01 #reasonable amount, anything less shouldn't be used
            self._best_list = np.array([1., 0., 0., 0., 1., 0., 0., 0. ,1.])
            self._best_inv_list = np.array([1., 0., 0., 0., 1., 0., 0., 0., 1.])
            self.fit = 100
            self.initial_fit = self.fit
            self.calibrate = False


    async def process_frame(self, frame, frame_ind):
        if self.calibrate:
            self._calibrate(frame, frame_ind)
        return

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
                print('updating homography...fit = {}'.format(self.fit))
                self._update_homography(frame)
                if (self.fit < .001 and self.fit < self.initial_fit):
                    self._write_calibration(SpatialCalibrationProcessor.PICKLE_FILE)
                self.calibration_points = np.random.random( (self.N, 2)) * 0.8 + 0.1
                #seed next round with fit, weighted by how well the homography fit
                self.points[:] = cv2.perspectiveTransform(self.calibration_points.reshape(-1,1,2), linalg.inv(self._transform)).reshape(-1,2)
                self.counts[:] = max(0, (0.01 - self.fit) * 10)
            self.index += 1
            self.index %= self.N

    def warp_img(self, img):
        #return cv2.warpPerspective(img, self._best_scaled_transform, (img.shape[1], img.shape[0]))
        for i in range(img.shape[2]):
            img[:,:,i] = cv2.warpPerspective(img[:,:,i],
                                             self._best_scaled_transform,
                                             img.shape[1::-1])
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
        p = cv2.perspectiveTransform((self.points).reshape(-1, 1, 2), self._transform).reshape(-1, 2)
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
        return  frame#cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    @property
    def objects(self):
        if self.calibrate:
            self._objects[0]['center_scaled'] = self.calibration_points[self.index]
            return self._objects
        return []

class BackgroundProcessor(Processor):
    '''Substracts and computes background'''
    def __init__(self, camera, background = None):
        super().__init__(camera, ['bg-view', 'bg-diff-blur'], 1)
        self.reset()
        self.pause()
        self._background = background
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
        #cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
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
            #self._background = cv2.blur(self._background, (5,5))
        return


    async def decorate_frame(self, frame, name):
        if name == 'bg-view':
            return self.background
        if name =='bg-diff-blur':
            return diff_blur(self.background, frame)
        return frame

class TrackerProcessor(Processor):

    @property
    def objects(self):
        '''Objects should have a dictionary with center, brect, name, and id'''
        if (self.dialReader is not None):
            return self.dialReader._objects + self._tracking
        else:
            return self._tracking


    def __init__(self, camera, detector_stride, background, delete_threshold_period=1.0, stride=2, detectLines = True, readDials = True, do_tracking = True, alpha=0.8):
        super().__init__(camera, ['track','line-segmentation'], stride)
        self._tracking = []
        self.do_tracking = do_tracking #this should only be False if we're using darkflow
        self.alpha = alpha
        self.labels = {}
        self.stride = stride
        self.ticks = 0
        if(do_tracking):
            self.optflow = cv2.DualTVL1OpticalFlow_create()#use dense optical flow to track
        self.detect_interval = 3
        self.prev_gray = None
        self.tracks = []
        self.min_pts_near = 4#the minimum number of points we need to say an object's center is here
        self.pts_dist_squared_th = int(75.0 / 2 / 720.0 * background.shape[0])**2
        self.feature_params = dict( maxCorners = 500,
                qualityLevel = 0.3,
                minDistance = 7,
                blockSize = 7 )
        print('initializing trackerprocessor. background.shape is {} by {}'.format(background.shape[0], background.shape[1]))
        self.dist_th_upper = int(150.0 / 720.0 * background.shape[0])# distance upper threshold, in pixels
        self.dist_th_lower = int(75.0 / 720.0 * background.shape[0]) # to account for the size of the reactor
        print('dist_th_upper is {} and dist_th_lower is {}'.format(self.dist_th_upper, self.dist_th_lower))
        self.max_obs_possible = 24
        # set up line detector
        if detectLines:
             self.lineDetector = LineDetectionProcessor(camera,stride,background)
        else:
            self.lineDetector = None
        # need to keep our own ticks because
        # we don't know frame index when track() is called
        if detector_stride > 0:
            self.ticks_per_obs = detector_stride * delete_threshold_period /self.stride

        if readDials:
            self.dialReader = DialProcessor(camera, stride=1)
        else:
            self.dialReader = None

    def close(self):
        super().close()
        if self.dialReader is not None:
            self.dialReader.close()

    async def process_frame(self, frame, frame_ind):
        self.ticks += 1
        delete = []

        if(self.do_tracking):
            smaller_frame = frame
            smaller_frame = smaller_frame#4x downsampling
            smaller_frame = cv2.cvtColor(smaller_frame, cv2.COLOR_BGR2GRAY)
            gray = smaller_frame#cv2.UMat(smaller_frame)
            if(self.prev_gray is None):
                self.prev_gray = gray#gray
                return
            img0, img1 = self.prev_gray, gray#gray
            #p0 = np.float32(self.tracks).reshape(-1, 1, 2)\
            p1 = self.optflow.calc(img0, img1, None)#cv2.calcOpticalFlowFarneback(img0, img1, None, 0.5, 2, 15, 2, 5, 1.1, 0)#, p0)#, None, **self.lk_params)  p1, _st, _err
            if(frame_ind % self.detect_interval == 0 or len(self.tracks)==0):
                mask = np.zeros((smaller_frame.shape), dtype=np.uint8)#np.zeros_like(gray)
                mask[:] = 255
                self.tracks = np.float32(cv2.goodFeaturesToTrack(smaller_frame, mask=mask, **self.feature_params)).reshape(-1,2)

        for i,t in enumerate(self._tracking):
            old_center = t['center_scaled']
            t['connectedToPrimary'] = [] # list of tracked objects it is connected to as the primary/source node
            t['connectedToSecondary'] = []
            t['connectedToSource'] = False
            #status,brect = t['tracker'].update(umat_frame)
            t['observed'] -= 1
            if(self.do_tracking):
                # we know our objects should stay the same size all of the time.
                # check if the size dramatically changed.  if so, the object most likely was removed
                # if not, rescale the tracked brect to the correct size
                #print("t['center_scaled'] is {}".format(t['center_scaled']))
                center_unscaled = (t['center_scaled'][0]*smaller_frame.shape[1] , t['center_scaled'][1]*smaller_frame.shape[0])
                #print('center_unscaled is {} and smaller_frame.shape is {}'.format(center_unscaled, smaller_frame.shape))
                #print('the dimensions of p1 are {}'.format(p1.shape))
                a = int(center_unscaled[1])
                b = int(center_unscaled[0])
                flow_at_center = [p1[a][b][0], p1[a][b][1]]#get the flow computed at previous center of object
                #flow_at_center = flow_at_center[::-1]#this is reversed for some reason..?
                flow_at_center = scale_point(flow_at_center, smaller_frame)
                print('flow_at_center is {}'.format(flow_at_center))
                dist = distance_pts([[0,0], flow_at_center ])#this is the magnitude of the vector
                # check if its new location is a reflection, or drastically far away
                near_pts = 0
                for pt in self.tracks:
                    if(distance_pts([center_unscaled, pt]) <= self.pts_dist_squared_th):
                        near_pts += 1
                if (dist < .05 * max(smaller_frame.shape) and near_pts >= 5):#don't move more than 5% of the biggest dimension
                    #print('Updated distance is {}'.format(dist))
                    # rescale the brect to match the original area?
                    t['center_scaled'][0] += flow_at_center[0]
                    t['center_scaled'][1] += flow_at_center[1]
                    t['observed'] = min(t['observed'] +2, self.max_obs_possible)
                #put note about it
            # check obs counts
            if t['observed'] < 0:
                delete.append(i)
        offset = 0
        delete.sort()
        for i in delete:
            for j,t in enumerate(self._tracking):
                # remove any references of this node from connectedToPrimary
                t2 = self._tracking[i-offset]
                if (t2['id'],t2['label']) in t['connectedToPrimary']:
                    index = t['connectedToPrimary'].index((t2['id'],t2['label']))
                    del t['connectedToPrimary'][index]
            del self._tracking[i - offset]
            offset += 1

        #update _tracking with the connections each object has
        await self._connect_objects(frame.shape)
        #f frame_ind % 4 * self.stride == 0:
        #    for t in self._tracking:
        #        print('{} is connected to ({})'.format(t['label'], t['connectedToPrimary']))
                #print('Is {} connected to the feed source? {}'.format(t['label'], t['connectedToSource']))
        if(self.do_tracking):
            self.prev_gray = gray
        return

    async def _connect_objects(self, frameSize):
        if (self.lineDetector is None) or len(self.lineDetector.lines) == 0:
            return
        ''' Iterates through tracked objects and the detected lines, finding objects are connected. Updates self._tracking to have directional knowledge of connections'''
        source_position_scaled = (1.0,0.5)#first coord is X from L to R, second coord is Y from TOP to BOTTOM
        source_position_unscaled = (frameSize[1],round(frameSize[0]*.5))
        #source_position_unscaled = self._unscale_point(source_position_scaled, frameSize)
        source_dist_thresh_upper = int(200.0 / 720.0 * frameSize[0])
        source_dist_thresh_lower = int(10.0 / 720.0 * frameSize[0])
        #print('source_dist_thresh_upper is {} and framesize[1] is {}'.format(source_dist_thresh_upper, frameSize[1]))
        used_lines = []
        for i,t1 in enumerate(self._tracking):
            center = self._unscale_point(t1['center_scaled'], frameSize)
            # find all lines that have an endpoint near the center of this object
            for k,line in enumerate(self.lineDetector.lines):
                if k in used_lines:
                    continue # dont attempt to use this line if it is already associated with something

                dist_ep1 = distance_pts((center, line['endpoints'][0]))
                dist_ep2 = distance_pts((center, line['endpoints'][1]))
                nearbyEndpointFound = False
                #print('Distances for {} {} (position {}) to the endpoints are {} (position {}) and {} (position {})'.format(t1['label'], t1['id'], center, min(dist_ep1,dist_ep2), line['endpoints'][0], max(dist_ep1,dist_ep2), line['endpoints'][1]))
                if (val_in_range(dist_ep1,self.dist_th_lower,self.dist_th_upper) or val_in_range(dist_ep2,self.dist_th_lower,self.dist_th_upper)):
                    # we have a connection! use the endpoint that is further away to find another object thats close to it

                    if (dist_ep1 <= dist_ep2):
                        # use endpoint 2
                        endpoint = line['endpoints'][1]
                        #print('{} at {} is close to {} with a distance of {}, using {} to detect a connection'.format(t1['name'], center, dist_ep1, line['endpoints'][0], line['endpoints'][1]))
                    else:
                        endpoint = line['endpoints'][0]
                        #print('{} at {} is close to {} with a distance of {}, using {} to detect a connection'.format(t1['name'], center, dist_ep2, line['endpoints'][1], line['endpoints'][0]))

                    # first check if the opposite endpoint is closest to the source
                    dist_source = distance_pts((source_position_unscaled, endpoint))
                    if (val_in_range(dist_source, source_dist_thresh_lower, source_dist_thresh_upper)):
                        # connected to the source
                        t1['connectedToSource'] = True
                        #print('Item {} is connected to the source'.format(t1['label']))
                        used_lines.append(k)
                        break
                    #else:
                        #print('Distance from source to endpoint was {}'.format(dist_source))
                    # iterate over all tracked objects again to see if the end of this line is close enough to any other object
                    for j,t2 in enumerate(self._tracking):
                        #print('made it this FAR!!! {} {}'.format(t1['id'], t2['id']))#now this DOES print for whatever reason...
                        if (t1['id'] == t2['id']):
                            # don't attempt to find connections to yourself
                            continue
                        # also don't attempt a connection if these two are already connected
                        if (((t2['id'], t2['label']) in t1['connectedToPrimary'])  or ((t2['id'], t2['label']) in t1['connectedToSecondary']) ):
                            continue

                        # check if the slope between the two rxrs and that of the line are similar
                        center2 = self._unscale_point(t2['center_scaled'], frameSize)
                        #print('the distance between objects {} {} and {} {} is {}'.format(t1['label'],t1['id'], t2['label'],t2['id'], distance_pts((center, center2))))#now this line is printing again...
                        lineSlope = line['slope']
                        lineAngle = np.pi/2.0 + np.arctan(lineSlope)#compare angles instead of slopes; bounded space from 0 to pi
                        rxrSlope, intercept = line_from_endpoints((center, center2)) if center[1] > center2[1] else line_from_endpoints((center2, center))
                        rxrAngle = np.pi/2.0 + np.arctan(rxrSlope)
                        angleDiff = abs(lineAngle - rxrAngle)#at most pi
                        #print('line angle is {} deg, rxr angle is {} deg, and angle % difference is {}'.format(lineAngle * 180./np.pi, rxrAngle * 180./np.pi, angleDiff/np.pi))#print in degrees for legibility
                        #sys.stdout.flush()
                        angleThresh = np.pi/6.0
                        if angleDiff > angleThresh:
                            continue
                        dist2 = distance_pts((center2, endpoint))
                        if (val_in_range(dist2, self.dist_th_lower,self.dist_th_upper)):
                            # its a connection! list this one as a connection, then break out of this loop
                            # we can create directionality by having two lists
                            # figure out which one is further to the left by checking the which x coordinate is greater (counter-intuitive, but the camera view is flipped)
                            # if equal, use the y coordinate
                            if (center[0] > center2[0]) or (center[0] == center2[0] and center[1] < center2[1]):
                                # first point is the primary
                                t1['connectedToPrimary'].append((t2['id'], t2['label']))
                                t2['connectedToSecondary'].append((t1['id'], t1['label']))
                            else:
                                t2['connectedToPrimary'].append((t1['id'],t1['label']))
                                t1['connectedToSecondary'].append((t2['id'], t2['label']))

                            #print('{} is connected to {}'.format(t1['name'], t2['name'])) #debug message
                            #print('Item {} is connected to {}'.format(t1['label'], t2['label']))

                            # make sure that the line used to discern this connection is not used again
                            used_lines.append(k)
                            break
                        else:
                            pass
                            #print('dist from object {} was {}'.format(t2['name'], dist2))



    def _unscale_point(self,point,shape): #TODO: move these to utils.py
        return (point[0]*shape[1], point[1]* shape[0])
    def _unscale(self, array, shape):
        return (array * [shape[1], shape[0]]).astype(np.int32)

    async def decorate_frame(self, frame, name):
        smaller_frame = frame
        if name == 'line-segmentation':
            frame = self.lineDetector.threshold_background(frame)
            return  frame#cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if name != 'track':
            return  smaller_frame#cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        for i,t in enumerate(self._tracking):
            if(t['observed'] < 3):
                continue
            center_pos = tuple(np.array(self._unscale_point(t['center_scaled'], frame.shape)).astype(np.int32))
            #print('the center position is {}'.format(center_pos))
            cv2.circle(frame,center_pos, 10, (0,0, 255), -1)#draw red dots at centers of each polygon
            #draw the inner and outer dist thresholds for linefinding
            cv2.circle(frame, center_pos, self.dist_th_lower, (0,255, 255), 2)#BGR for yellow
            cv2.circle(frame, center_pos, self.dist_th_upper, (255,255, 0), 2)#BGR for cyan
            cv2.putText(frame,
                        '{}: {}'.format(t['name'], t['observed']),
                        (0, 60 * (i+ 1)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0,255,255))

        # view lines, as the decorator for LineProcessor is not working
        if (self.lineDetector is not None):
            for i,line in enumerate(self.lineDetector.lines):
                endpoints = line['endpoints']
                # place dots at each existing endpoint. blue for 0, green for 1
                cv2.circle(frame, (endpoints[0][0],endpoints[0][1]), 3 , (255,0,0), -1)#BGR
                cv2.circle(frame, (endpoints[1][0],endpoints[1][1]), 3 , (0,255,0), -1)#BGR

        return  frame#cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    def track(self, frame, brect, poly, label, id_num, temperature = 298):
        '''
        Track a newly found object (returns True), or return False for an existing object.
        '''

        center = poly_scaled_center(poly, frame) if (poly is not None and cv2.contourArea(poly) < rect_area(brect)) else rect_scaled_center(brect, frame)
        # get current value of temperature - this will not change after the initialization
        if (self.dialReader is not None):
            temperature = self.dialReader.temperature
        else:
            temperature = 298
        #we need to make sure we don't have an existing object here
        for t in self._tracking:
            if  t['name'] == '{}-{}'.format(label, id_num) or intersecting_rects(t['brect'], brect): #found already existing reactor
                t['observed'] = self.ticks_per_obs
                t['center_scaled'] = [t['center_scaled'][0] * (1.0 - self.alpha) + center[0] * self.alpha, t['center_scaled'][1] * (1.0 - self.alpha) + center[1] * self.alpha] #do exponential averaging of position to cut down jitters
                t['brect'] = brect
                return False


        name = '{}-{}'.format(label, id_num)
        #tracker = cv2.DualTVL1OpticalFlow_create()
        #status = tracker.init(cv2.UMat(frame), brect)

        #if not status:
        #    print('Failed to initialize tracker')
        #    return False



        track_obj = {'name': name,
                     #'tracker': tracker,
                     'label': label,
                     'poly': poly,
                     'init': brect,
                     'area_init':rect_area(brect),
                     'center_scaled': poly_scaled_center(poly, frame) if (poly is not None and cv2.contourArea(poly) < rect_area(brect)) else rect_scaled_center(brect, frame),
                     'brect': brect,
                     'observed': self.ticks_per_obs,
                     'start': self.ticks,
                     'delta': np.int32([0,0]),
                     'id': id_num,
                     'connectedToPrimary': [],
                     'weight':[temperature,1]}
        self._tracking.append(track_obj)
        return True

class SegmentProcessor(Processor):
    def __init__(self, camera, background, stride, max_segments, max_rectangle=0.25, channel=None, hsv_delta=[100, 110, 16], name=None):#TODO: mess with this max_rectangle and see if that helps the big brect isues
        '''Pass stride = -1 to only process on request'''
        if(name is None):
            self.name_str = ''
        else:
            self.name_str = name
        super().__init__(camera, [

                                  self.name_str + 'bg-subtract',
                                  self.name_str + 'bg-filter-blur',
                                  self.name_str + 'bg-thresh',
                                  self.name_str + 'bg-erode',
                                  self.name_str + 'bg-open',
                                  self.name_str + 'distance',
                                  self.name_str + 'boxes',
                                  self.name_str + 'watershed'
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
            self._process_frame(frame, frame_ind)
            return
        return

    def _process_frame(self, frame, frame_ind):
        bg = self._filter_background(frame)
        dist_transform = self._filter_distance(bg)
        self.rect_iter = self._filter_contours(dist_transform, frame.shape)
        return

    def segments(self, frame = None):
        if frame is not None:
            self._process_frame(frame, 0)
        yield from self.rect_iter


    def _filter_background(self, frame, name = ''):

        img = frame#.copy()
        gray = cv2.UMat(img)
        #print('frame is type {} and self.background is type {}'.format(frame, self.background))
        if(self.background is not None):
            gray = diff_blur(self.background, frame, False)
        if name.find('bg-subtract') != -1:
            return gray
        if self.channel is None or True:
            if len(img.shape) == 3 and img.shape[2] == 3:
                gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        else:
            gray = gray#cv2.inRange(gray, self.hsv_min, self.hsv_max)
        gray = cv2.blur(gray, (5,5))
        if name.find('bg-filter-blur') != -1:
            return gray
        ret, bg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        #bg = cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        #                                cv2.THRESH_BINARY_INV,11,2)
        if np.mean(cv2.mean(bg)) > 255 // 2:
           bg = cv2.subtract(bg, 255)

        if name.find('bg-thresh') != -1:
            return bg
        # noise removal
        kernel = np.ones((4,4),np.uint8)
        bg = cv2.erode(bg, kernel, iterations = 1)
        if name.find('bg-erode') != -1:
            return bg
        bg = cv2.morphologyEx(bg,cv2.MORPH_OPEN,kernel, iterations = 1)
        if name.find('bg-open') != -1:
            return bg

        return bg

    def _filter_distance(self, frame):
        dist_transform = cv2.distanceTransform(frame, cv2.DIST_L2,5)
        dist_transform = cv2.normalize(dist_transform, dist_transform, 0, 255, cv2.NORM_MINMAX)

        #create distance tranform contours
        #dist_transform = np.uint8(cv2.UMat.get(dist_transform))
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

    def sort_key(self, c):
        '''Get the area of a bounding rectangle'''
        rect = cv2.boundingRect(c)
        return rect[2] * rect[3]

    def _filter_contours(self, frame, frame_shape, return_contour=False):
        _, contours, _ = cv2.findContours(frame, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours.sort(key = self.sort_key, reverse=True)
        rects = [cv2.boundingRect(c) for c in contours]
        segments = 0

        for c,r in zip(contours, rects):
            #flip around our rectangle
            # exempt small or large rectangles
            if(r[2] * r[3] < 250 or \
                r[2] * r[3] / frame_shape[0] / frame_shape[1] > self.max_rectangle ):
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
        if self.name_str not in name:
            return frame
        bg = self._filter_background(frame, name)


        dist_transform = self._filter_distance(bg)
        if 'distance' in name:
            return dist_transform

        if 'boxes' in name:
            for rect in self._filter_contours(dist_transform, frame.shape):
                draw_rectangle(frame, rect, (255, 255, 0), 1)
        if 'watershed' in name:
            markers = self._filter_ws_markers(dist_transform)
            ws_markers = cv2.watershed(frame, markers)
            frame[ws_markers == -1] = (255, 0, 0)
        return frame#cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


class TrainingProcessor(Processor):

    @property
    def objects(self):
        '''Objects should have a dictionary with center, bbrect, name, and id'''
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
            self.rect = self.segments[self.rect_index]#stretch_rectangle(self.segments[self.rect_index], frame)

        # index 1 is poly
        self.polys = [x[0] for x in self.segmenter.polygon(frame, self.rect)]
        self.poly_len = len(self.polys)
        if self.poly_index >= 0 and self.poly_index < len(self.polys):
            self.poly = self.polys[self.poly_index]

        return

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

        return frame#cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

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
            'brect': self.rect,
            'poly': self.poly,
            'center_scaled': poly_scaled_center(self.poly, frame) if cv2.contourArea(self.poly) < rect_area(self.rect) else rect_scaled_center(self.rect, frame),
            'label': label,
            'id': object_id()
        }]

        return True


class DetectionProcessor(Processor):
    '''Detects query images in frame. Uses async to spread out computation. Cannot handle replicas of an object in frame'''
    def __init__(self, camera, background, img_db, descriptor, stride=3,
                 threshold=0.8, template_size=256, min_match=6,
                 weights=[3, -1, -1, -10, 5], max_segments=10,
                 track=True):

        #we have a specific order required
        #set-up our tracker
        # give estimate of our stride
        if track:
            self.tracker = TrackerProcessor(camera, stride * 2 * len(img_db), background)
        else:
            self.tracker = None


        #then our segmenter
        self.segmenter = SegmentProcessor(camera, background, -1, max_segments)
        #then us
        super().__init__(camera, ['keypoints', 'identify'], stride)


        self._ready = True
        self.features = {}
        self.threshold = threshold#this is the percentage of distance similarity
        self.min_match = min_match
        self.weights = weights
        self.stretch_boxes=1.5
        self.track = track
        self.templates = img_db
        self.stride = stride

        # Initiate descriptors
        self.desc = descriptor
        FLANN_INDEX_KDTREE = 0
        index_params = dict(algorithm = FLANN_INDEX_KDTREE, trees = 5)
        search_params = dict(checks=50)
        self.matcher = cv2.FlannBasedMatcher(index_params,search_params)#cv2.BFMatcher()

        #create color gradient
        N = len(img_db)
        for i,t in enumerate(self.templates):
            rgba = [int(x * 255) for x in np.random.random(size=4)]
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
            asyncio.ensure_future(self._identify_features(frame, frame_ind))
        return

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
                color = f['color']
                kp = f['kp']
                kpcolor = f['kpcolor']
                for p,c in zip(kp, kpcolor):
                    cv2.circle(frame, tuple(p), 6, color, thickness=-1)

        return  frame#cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    async def _identify_features(self, frame, frame_ind):
        self._ready = False
        #make new features object
        features = {}

        found_feature = False
        for rect in self.segmenter.segments(frame):
            kp, des = keypoints_view(self.desc, frame, rect)
            if(des is not None and len(des) > 3):
                rect_features = await self._process_frame_view(frame, kp, des, rect, frame_ind)
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

    async def _process_frame_view(self, frame, kp, des, bounds, frame_ind):
        '''This method tries to run the calculation over multiple loops.
            The _ready is to in lieu of a callback on completion'''
        self._ready = False
        features = {}
        for t in self.templates:
            # check if t is already in play by its id number
            # if yes, check the frame index and see if this index is 2x the stride.
            templateInPlay = False
            for o in self.objects:
                if o['id'] == t.id:
                    templateInPlay = True

            if (templateInPlay and (frame_ind % (self.stride*2) != 0)):
                continue

            try:
                template = t.img
                name = t.label
                descriptors = t.keypoints, t.features

                if(type(descriptors[1]) != np.float32):
                    des1 = np.float32(descriptors[1])
                if(type(des) != np.float32):
                    des2 = np.float32(des)
                matches = self.matcher.knnMatch(des1, des2, k=2)
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
                    dst_brect = cv2.boundingRect(dst_poly)

                    # check if the polygon is actually good
                    area = max(0.01, cv2.contourArea(dst_poly))
                    perimter = max(0.01, cv2.arcLength(dst_poly, True))
                    score = len(good) / len(des) * self.weights[0] + \
                            perimter / area * self.weights[1] + \
                            (dst_brect[2] / bounds[2] - 1 + dst_brect[3] /  bounds[3] - 1) * self.weights[2] + \
                            (dst_brect[2] * dst_brect[3] < 5) * self.weights[3] + \
                            self.weights[4]
                    if score > 0:
                        features[name] = { 'color': t.color, 'poly': np.int32(dst_poly),
                            'kp': np.int32([kp[m.trainIdx].pt for m in good]).reshape(-1,2),
                            'kpcolor': [(255, 255, 255, 128) for x in good],
                            'score': score, 'rect': bounds}
                        # register it with our tracker
                        if self.track:
                            self.tracker.track(frame, bounds, np.int32(dst_poly), name, t.id)
            except cv2.error:
                #not enough points
                await asyncio.sleep(0)
                continue

            #cede control
            await asyncio.sleep(0)
        return features

class DarkflowSegmentProcessor(Processor):
    def __init__(self, camera, stride=1, threshold=0.1):
        self.tfnet = load_darkflow('dot-tracking', gpu=1.0, threshold=threshold)
        super().__init__(camera, ['segment'], stride)

    def segments(self, frame):
        return self._process_frame(frame)

    def _process_frame(self, frame):
        result = self.tfnet.return_predict(frame) #get a dict of detected items with labels and confidences.
        sorted_result = sorted(result, key=lambda x: x['confidence'], reverse=True)
        segments = [darkflow_to_rect(x) for x in sorted_result]
        return segments

    async def process_frame(self, frame, frame_id):
        return

    async def decorate_frame(self, frame, name):
        if name == 'segment':
            for s in self.segments(frame):
                draw_rectangle(frame, s, (255, 255, 0), 1)

class DarkflowDetectionProcessor(Processor):
    '''Detects query images in frame. Uses async to spread out computation. Cannot handle replicas of an object in frame'''
    def __init__(self, camera, background, stride=3,
                 threshold=0.1, track=True):
        self.tfnet = load_darkflow('reactor-tracking', gpu=1.0, threshold=threshold)
        self.id_i = 1000#skip over 0 thru 999
        #we have a specific order required
        #set-up our tracker
        # give estimate of our stride
        if track:
            self.tracker = TrackerProcessor(camera, stride,  background,  delete_threshold_period=5, do_tracking = False)
        else:
            self.tracker = None

        #then us
        super().__init__(camera, ['identify'], stride)
        self.track = track
        self.stride = stride

    @property
    def objects(self):
        if self.tracker is None:
            return []
        return self.tracker.objects

    def close(self):
        super().close()
        self.tracker.close()

    async def process_frame(self, frame, frame_ind):
        result = self.tfnet.return_predict(frame)#get a dict of detected items with labels and confidences.
        for item in result:
            brect = darkflow_to_rect(item)
            label = item['label']
            id_num = self.id_i
            new_obj = self.tracker.track(frame, brect, None, label, id_num)
            if(new_obj):
                self.id_i += 1

        return

    async def decorate_frame(self, frame, name):
        for i,item in enumerate(self.tracker._tracking):
            draw_rectangle(frame, item['brect'], (255, 0, 0), 1)
            (x,y) = rect_center(item['brect'])
            cv2.circle(frame, (int(x),int(y)), 10, (0,0,255), -1)
            cv2.putText(frame,
                        '{}: {}'.format(item['name'], item['observed']),
                        (0, 60 * (i+1)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0,255,255))
        return frame


class LineDetectionProcessor(Processor):
    ''' Detects drawn lines on an image (NB: works with a red marker or paper strip)
        This will not return knowledge of connections between reactors (that logic should be in DetectionProcessor or TrackerProcessor, which this class should be controlled by)
    '''
    def __init__(self, camera, stride, background, obsLimit = 5):
        super().__init__(camera, ['image-segmented','lines-detected'],stride)
        self._lines = [] # initialize as an empty array - list of dicts that contain endpoints, slope, intercept
        # preprocess the background image to help with raster noise
        #cv2.bilateralFilter(background, 7, 150, 150) #switched to median b/c bilateral preserves edges which is not what we want
        self._background = cv2.blur(cv2.medianBlur(background, 5), (7,7))
        self._ready = True
        self._observationLimit = obsLimit # how many failed calculations/countdowns until we remove a line
        self._stagedLines = [] # lines that were detected in the previous call to process_frame.  if they are detected again, add them to the main line list

    @property
    def lines(self):
        return self._lines

    async def process_frame(self, frame, frame_ind):
        if(self._ready):
            #copy the frame into it so we don't have it processed by later methods
            asyncio.ensure_future(self.detect_adjust_lines(frame.copy()))
        return

    async def decorate_frame(self, frame, name):
        smaller_frame = frame
        if name != 'image-segmented' or name != 'lines-detected':
            return  smaller_frame#cv2.cvtColor(smaller_frame, cv2.COLOR_BGR2GRAY)

        if name == 'image-segmented':
            bg = self.threshold_background(frame)
            return  bg#cv2.cvtColor(smaller_frame, cv2.COLOR_BGR2GRAY)

        # if name == 'lines-detected':
        #     print('Adding the points')
        # add purple points for each endpoint detected
        for i in range(0,len(self._lines)):
            cv2.circle(frame, (self._lines[i][0][0], self._lines[i][0][1]), (255,0,255),-1)
            cv2.circle(frame, (self._lines[i][1][0], self._lines[i][1][1]), (255,0,255),-1)

        return  frame#cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


    '''
    Use _detect_lines to get currently found lines, and compare to the previously found ones.  Adjust/add/remove from _lines property
    Should return nothing, but updates the lines property
    '''
    async def detect_adjust_lines(self,frame):
        self._ready = False
        detected_lines = self._detect_lines(frame)#list of tuples of pair-tuples (the line endpoint coords)
        # need a way to remove previous lines that were not found.
        currentLines = self._lines
        # empty out self._lines.
        self._lines = []
        leftoverLines = [] # lines that did not match previously staged or currently held lines.  this becomes the next staged lines
        for i in range(0,len(currentLines)):
            # add/adjust a value that indicates if the current line was detected in this latest frame
            currentLines[i]['detected'] = False

        for i in range(0,len(detected_lines)):
            detectedEndpoints = detected_lines[i]
            (detectedSlope, detectedIntercept ) = line_from_endpoints(detectedEndpoints)
            # iterate through existing lines - we should store their slope and intercept
            mainLineUpdated = False

            # if self._lines is empty, it should just add the new line
            for j in range(0,len(currentLines)):
                existingLine = currentLines[j]

                currentSlope,currentIntercept = line_from_endpoints(existingLine['endpoints'])
                if(abs(percent_diff(currentSlope,detectedSlope)) < 0.15 and abs(percent_diff(currentIntercept,detectedIntercept)) < .15):
                    # easy way out - take the longer of the two lines. future implementations should check if they should be overlapped and take the longest line combination from them
                    if (distance_pts(detectedEndpoints) > distance_pts(existingLine['endpoints'])):
                        # replace with the new line
                        currentLines[j] = {'endpoints':detectedEndpoints, 'slope':detectedSlope, 'intercept':detectedIntercept, 'detected':True, 'observed':self._observationLimit}
                    else:
                        # update an existing line, so we know it was actually found
                        currentLines[j]['detected'] = True
                        # restart its countdown
                        currentLines[j]['observed'] = self._observationLimit
                    #however, at this stage, with whatever happens, we should stop iterating over existing lines
                    mainLineUpdated = True
                    break

            if not mainLineUpdated:
                # check if this line matches a staged line. if yes, increment its matching staged line's counter.  if not, add it as a new staged line
                stagedLineUpdated = False
                for j in range(0, len(self._stagedLines)):
                    stagedLine = self._stagedLines[j]
                    stagedSlope = stagedLine['slope']
                    stagedIntercept = stagedLine['intercept']
                    if (abs(percent_diff(stagedSlope,detectedSlope)) < 0.15 and abs(percent_diff(stagedIntercept, detectedIntercept)) < 0.15):
                        stagedLineUpdated = True
                        if(distance_pts(detectedEndpoints) > distance_pts(stagedLine['endpoints'])):
                            self._stagedLines[j] = {'endpoints':detectedEndpoints, 'slope':detectedSlope, 'intercept':detectedIntercept, 'detected':True, 'observed':self._observationLimit}
                        else:
                            self._stagedLines[j]['detected'] = True # staged does not decrement the observation limit, so no need to reset it
                        # stop iterating over staged lines
                        break
                if not stagedLineUpdated:
                    leftoverLines.append({'endpoints':detectedEndpoints, 'slope':detectedSlope, 'intercept':detectedIntercept, 'detected':False, 'observed':self._observationLimit})

        # one last passthrough, only adding lines that were re-detected or new
        for i in range(0,len(currentLines)):
            lineDict = currentLines[i]
            if (lineDict['detected']):
                self._lines.append(lineDict)

            else:
                # the line was not observed.  decrement the countdown, and only add it into our tracked lines if the observed value is greater than 0
                lineDict['observed'] -= 1
                if (lineDict['observed'] > 0):
                    self._lines.append(lineDict)

        # add any staged lines to the main list if they were detected again. set the leftover lines to be the new staged lines
        for i in range(0,len(self._stagedLines)):
            lineDict = self._stagedLines[i]
            if (lineDict['detected']):
                self._lines.append(lineDict)
        self._stagedLines = leftoverLines
        self._ready = True



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
                area_thresh_upper = 0.02 * frame.shape[0] * frame.shape[1]
                area_thresh_lower = 0.0002 * frame.shape[0] * frame.shape[1]
                width_thresh = 0.04 * frame.shape[0]
                length_thresh_lower = 0.05 * frame.shape[0]
                length_thresh_upper = 0.4 * frame.shape[0]
                if (aspectRatio < aspect_ratio_thresh and val_in_range(area, area_thresh_lower, area_thresh_upper) and minDim < width_thresh and val_in_range(maxDim, length_thresh_lower, length_thresh_upper)):
                    # only keep endpoints if it is the correct shape
                    endpoints = rect_to_endpoints(rect)
                    lines.append(endpoints)

        return lines


    def threshold_background(self,frame):
        sum_diff = diff_blur(self._background, frame)
        # threshold this value- play with thresh_val in prod
        thresh_val = 45
        _,mask = cv2.threshold(sum_diff, thresh_val, 255, cv2.THRESH_BINARY)
        # apply a sharpening filter
        kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
        mask = cv2.filter2D(mask,-1,kernel)
        return mask

    def isLineSimilar(detectedEndpoints,currentEndpoints):
        detectedSlope,detectedIntercept = line_from_endpoints(detectedEndpoints)
        currentSlope,currentIntercept = line_from_endpoints(currentEndpoints)
        if(math.abs(percent_diff(currentSlope,detectedSlope)) < 0.05 and math.abs(percent_diff(currentIntercept,detectedIntercept)) < .05):
            return True
        else:
            return False


class DialProcessor(Processor):
    ''' Class to handle sending the pressure and temperature data to the graph. Does no image processing '''
    def __init__(self, camera, stride =1, initialTemperatureValue = 300, temperatureStep = 5, tempLowerBound = 100, tempUpperBound = 800, debug = False):
        # assuming
        # set stride low because we have no image processing to take time
        super().__init__(camera, [], stride)
        self.initTemp = initialTemperatureValue
        self.tempStep = float(temperatureStep)
        self.debug = debug
        self.tempLowerBound = tempLowerBound
        self.tempUpperBound = tempUpperBound
        self.reset()


    def reset(self):
        self.temperatureHandler = None
        self.pressureHandler = None
        try:
            from .griffin_powermate import GriffinPowermate,DialHandler
            devices = GriffinPowermate.find_all()
            if len(devices) == 0:
                self.temperatureHandler = None
                print('ERROR: FOUND NO DEVICES')
            else :
                self.temperatureHandler = DialHandler(devices[0], self.initTemp, self.tempStep, self.tempLowerBound,self.tempUpperBound)
        except ModuleNotFoundError:
            self.temperatureHandler = None
            print('ERROR: NO DIAL ON LINUX')



        # initialize the objects- give them constant ID#s
        self._objects = [{'id': CONDITIONS_ID, 'label': 'conditions', 'weight':[self.initTemp,1]}]

    @property
    def temperature(self):
        return self._objects[0]['weight'][0]

    async def process_frame(self, frame, frame_ind):
        # we're going to ignore the frame, just get the values from the dial handlers
        if self.temperatureHandler is not None:
            for o in self._objects:
                if o['label'] is 'conditions': # in case we ever wanted to do other work with the dials, leaving the framework in place to handle multiples
                    o['weight'] = [self.temperatureHandler.value,1]

        if (self.debug and frame_ind % 100 == 0):
            print('DEBUG: Current Temperature is {} K'.format(self.temperature))
        return

    async def decorate_frame(self, frame, name):
        # Not going to do anything here
        return frame

    def close(self):
        super().close()
        if self.temperatureHandler is not None:
            self.temperatureHandler.close()
        if self.pressureHandler is not None:
            self.pressureHandler.close()

    def play(self):
        if self.temperatureHandler is not None:
            self.temperatureHandler.play()
        if self.pressureHandler is not None:
            self.pressureHandler.play()

    def pause(self):
        if self.temperatureHandler is not None:
            self.temperatureHandler.pause()
        if self.pressureHandler is not None:
            self.pressureHandler.pause()

