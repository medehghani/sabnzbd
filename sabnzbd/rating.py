#!/usr/bin/python -OO
# Copyright 2008-2012 The SABnzbd-Team <team@sabnzbd.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
sabnzbd.rating - Rating support functions
"""

import httplib
import urllib
import time
import logging
import copy
import socket
import random
try:
    socket.ssl
    _HAVE_SSL = True
except:
    _HAVE_SSL = False
from threading import *

import sabnzbd
from sabnzbd.decorators import synchronized
from sabnzbd.misc import OrderedSetQueue
import sabnzbd.cfg as cfg

RATING_URL = "/releaseRatings/releaseRatings.php"
RATING_LOCK = RLock()

_g_warnings = 0
def _warn(msg):
    global _g_warnings
    _g_warnings += 1
    if _g_warnings < 3:
        logging.warning(msg)

def _reset_warn():
    global _g_warnings
    _g_warnings = 0

class NzbRating(object):
    def __init__(self):
        self.avg_video = 0
        self.avg_video_cnt = 0
        self.avg_audio = 0
        self.avg_audio_cnt = 0
        self.avg_vote_up = 0
        self.avg_vote_down = 0
        self.user_video = None
        self.user_audio = None
        self.user_vote = None
        self.user_flag = {}
        self.auto_flag = {}
        self.changed = 0
        
class Rating(Thread):
    VERSION = 1

    VOTE_UP = 1
    VOTE_DOWN = 2

    FLAG_OK = 0
    FLAG_SPAM = 1
    FLAG_ENCRYPTED = 2
    FLAG_EXPIRED = 3
    FLAG_OTHER = 4
    FLAG_COMMENT = 5
 
    CHANGED_USER_VIDEO = 0x01
    CHANGED_USER_AUDIO = 0x02
    CHANGED_USER_VOTE = 0x04
    CHANGED_USER_FLAG = 0x08
    CHANGED_AUTO_FLAG = 0x10

    do = None
    
    def __init__(self):
        Rating.do = self
        self.shutdown = False
        self.queue = OrderedSetQueue()
        try:
            (self.version, self.ratings, self.nzo_indexer_map) = sabnzbd.load_admin("Rating.sab")
            if (self.version != Rating.VERSION):
                raise Exception()
        except:
            self.version = Rating.VERSION
            self.ratings = {}
            self.nzo_indexer_map = {}
        Thread.__init__(self)
        if not _HAVE_SSL:
            logging.warning('Ratings server requires secure connection')
            self.stop()
        
    def stop(self):
        self.shutdown = True
        self.queue.put(None) # Unblock queue

    def run(self):
        self.shutdown = False
        while not self.shutdown:
            time.sleep(0.5)
            indexer_id = self.queue.get()
            try:
                if indexer_id and not self._send_rating(indexer_id):
                    for i in range(0, 60):
                        if self.shutdown: break
                        time.sleep(1)
                    self.queue.put(indexer_id)
            except:
                pass
        logging.debug('Stopping ratings')
             
    @synchronized(RATING_LOCK)
    def save(self):
        if self.ratings and self.nzo_indexer_map:
            sabnzbd.save_admin((self.version, self.ratings, self.nzo_indexer_map), "Rating.sab")

    # The same file may be uploaded multiple times creating a new nzo_id each time
    @synchronized(RATING_LOCK)
    def add_rating(self, indexer_id, nzo_id, video, video_cnt, audio, audio_cnt, vote_up, vote_down):
        if indexer_id and nzo_id and (video or audio or vote_up or vote_down):
            logging.debug('Add rating (%s, %s: %s, %s, %s, %s)', indexer_id, nzo_id, video, audio, vote_up, vote_down)
            try:
                rating = self.ratings.get(indexer_id, NzbRating())
                if video and video_cnt:
                    rating.avg_video = int(float(video))
                    rating.avg_video_cnt = int(float(video_cnt))
                if audio and audio_cnt:
                    rating.avg_audio = int(float(audio))
                    rating.avg_audio_cnt = int(float(audio_cnt))
                if vote_up: rating.avg_vote_up = int(float(vote_up))
                if vote_down: rating.avg_vote_down = int(float(vote_down))
                self.ratings[indexer_id] = rating
                self.nzo_indexer_map[nzo_id] = indexer_id
            except:
                pass

    @synchronized(RATING_LOCK)
    def update_user_rating(self, nzo_id, video, audio, vote, flag, flag_detail = None):
        logging.debug('Updating user rating (%s: %s, %s, %s, %s)', nzo_id, video, audio, vote, flag)
        if nzo_id not in self.nzo_indexer_map:
            logging.warning('indexer id (%s) not found for ratings file', nzo_id)
            return
        indexer_id = self.nzo_indexer_map[nzo_id]
        rating = self.ratings[indexer_id]
        if video:
            rating.user_video = int(video)
            rating.avg_video = int((rating.avg_video_cnt * rating.avg_video + rating.user_video) / (rating.avg_video_cnt + 1))
            rating.changed = rating.changed | Rating.CHANGED_USER_VIDEO
        if audio:
            rating.user_audio = int(audio)
            rating.avg_audio = int((rating.avg_audio_cnt * rating.avg_audio + rating.user_audio) / (rating.avg_audio_cnt + 1))  
            rating.changed = rating.changed | Rating.CHANGED_USER_AUDIO
        if flag:
            rating.user_flag = { 'val': int(flag), 'detail': flag_detail }
            rating.changed = rating.changed | Rating.CHANGED_USER_FLAG
        if vote and not rating.user_vote:
            rating.user_vote = int(vote)
            rating.changed = rating.changed | Rating.CHANGED_USER_VOTE
            if rating.user_vote == Rating.VOTE_UP:
                rating.avg_vote_up += 1 
            else:
                rating.avg_vote_down += 1        
        self.queue.put(indexer_id)

    @synchronized(RATING_LOCK)
    def update_auto_flag(self, nzo_id, flag, flag_detail = None):
        if not flag or not cfg.rating_feeback():
            return
        logging.debug('Updating auto flag (%s: %s)', nzo_id, flag)
        if nzo_id not in self.nzo_indexer_map:
            logging.warning('indexer id (%s) not found for ratings file', nzo_id)
            return
        indexer_id = self.nzo_indexer_map[nzo_id]
        rating = self.ratings[indexer_id]    
        rating.auto_flag = { 'val': int(flag), 'detail': flag_detail }
        rating.changed = rating.changed | Rating.CHANGED_AUTO_FLAG
        self.queue.put(indexer_id)

    @synchronized(RATING_LOCK)
    def get_rating_by_nzo(self, nzo_id):
        if nzo_id not in self.nzo_indexer_map:
             return None
        return copy.copy(self.ratings[self.nzo_indexer_map[nzo_id]])

    @synchronized(RATING_LOCK)
    def _get_rating_by_indexer(self, indexer_id):
        return copy.copy(self.ratings[indexer_id])

    def _flag_request(self, val, flag_detail, auto):
        if val == Rating.FLAG_SPAM:
            return {'m': 'rs', 'auto': auto}
        if val == Rating.FLAG_ENCRYPTED:
            return {'m': 'rp', 'auto': auto}
        if val == Rating.FLAG_EXPIRED:
            expired_host = flag_detail if flag_detail and len(flag_detail) > 0 else 'Other'
            return {'m': 'rpr', 'pr': expired_host, 'auto': auto};            
        if (val == Rating.FLAG_OTHER) and flag_detail and len(flag_detail) > 0:
            return {'m': 'o', 'r': flag_detail};
        if (val == Rating.FLAG_COMMENT) and flag_detail and len(flag_detail) > 0:
            return {'m': 'rc', 'r': flag_detail};
        
    def _send_rating(self, indexer_id): 
        logging.debug('Updating indexer rating (%s)', indexer_id)

        api_key = cfg.rating_api_key()
        rating_host = cfg.rating_host()
        if not api_key or not rating_host:
            return False
        
        requests = []
        _headers = {'User-agent' : 'SABnzbd+/%s' % sabnzbd.version.__version__, 'Content-type': 'application/x-www-form-urlencoded'}
        rating = self._get_rating_by_indexer(indexer_id) # Requesting info here ensures always have latest information even on retry
        if rating.changed & Rating.CHANGED_USER_VIDEO:
            requests.append({'m': 'r', 'r': 'videoQuality', 'rn': rating.user_video})
        if rating.changed & Rating.CHANGED_USER_AUDIO:
            requests.append({'m': 'r', 'r': 'audioQuality', 'rn': rating.user_audio})
        if rating.changed & Rating.CHANGED_USER_VOTE:
            up_down = 'up' if rating.user_vote == Rating.VOTE_UP else 'down'
            requests.append({'m': 'v', 'v': up_down, 'r': 'overall'})
        if rating.changed & Rating.CHANGED_USER_FLAG:
            requests.append(self._flag_request(rating.user_flag.get('val'), rating.user_flag.get('detail'), 0))
        if rating.changed & Rating.CHANGED_AUTO_FLAG:
            requests.append(self._flag_request(rating.auto_flag.get('val'), rating.auto_flag.get('detail'), 1))
        
        try:
            conn = httplib.HTTPSConnection(rating_host)
            for request in filter(lambda r: r is not None, requests):    
                request['apikey'] = api_key 
                request['i'] =  indexer_id
                conn.request('POST', RATING_URL, urllib.urlencode(request), headers = _headers)

                response = conn.getresponse()
                response.read()
                if response.status == httplib.UNAUTHORIZED:
                    _warn('Ratings server unauthorized user')
                    return False
                elif response.status != httplib.OK:
                    _warn('Ratings server failed to process request (%s, %s)' % response.status, response.reason)
                    return False

            rating.changed = 0
            _reset_warn()
            return True
        except:
            _warn('Problem accessing ratings server: %s' % rating_host)
            return False