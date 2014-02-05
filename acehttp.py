#!/usr/bin/python
# -*- coding: utf-8 -*-
'''
AceProxy: Ace Stream to HTTP Proxy

Website: https://github.com/ValdikSS/AceProxy
'''
import gevent
import gevent.monkey
# Monkeypatching and all the stuff
gevent.monkey.patch_all()
import gevent.queue
import glob
import os
import sys
import logging
import BaseHTTPServer
import SocketServer
import urllib2
import hashlib
import aceclient
from aceconfig import AceConfig
import vlcclient
from aceclient.clientcounter import ClientCounter
from plugins.PluginInterface import AceProxyPlugin


class HTTPHandler(BaseHTTPServer.BaseHTTPRequestHandler):

    def closeConnection(self):
        '''
        Disconnecting client
        '''
        if self.clientconnected:
            self.clientconnected = False
            try:
                self.wfile.close()
                self.rfile.close()
            except:
                pass

    def dieWithError(self, errorcode=500):
        '''
        Close connection with error
        '''
        logging.warning("Dying with error")
        if self.clientconnected:
            self.send_error(errorcode)
            self.end_headers()
            self.closeConnection()

    def proxyReadWrite(self):
        '''
        Read video stream and send it to client
        '''
        logger = logging.getLogger('http_proxyReadWrite')
        logger.debug("Started")

        self.vlcstate = True
        while True:
            try:
                if AceConfig.videoobey and not AceConfig.vlcuse:
                    # Wait for PlayEvent if videoobey is enabled. Not for VLC
                    self.ace.getPlayEvent()

                if AceConfig.videoobey and AceConfig.vlcuse:
                    # For VLC
                    try:
                        # Waiting 0.5 seconds. If timeout, there would be exception.
                        # Set vlcstate to False in the exception and pause the
                        # stream
                        # A bit ugly, huh?
                        self.ace.getPlayEvent(0.5)
                        if not self.vlcstate:
                            AceStuff.vlcclient.unPauseBroadcast(self.vlcid)
                            self.vlcstate = True
                    except gevent.Timeout:
                        if self.vlcstate:
                            AceStuff.vlcclient.pauseBroadcast(self.vlcid)
                            self.vlcstate = False

                if not self.clientconnected:
                    logger.debug("Client is not connected, terminating")
                    return

                data = self.video.read(4096)
                if data and self.clientconnected:
                    self.wfile.write(data)
                else:
                    # Prevent 100% CPU usage
                    gevent.sleep(0.5)
            except:
                # Video connection dropped
                logger.debug("Video Connection dropped")
                self.video.close()
                self.closeConnection()
                gevent.sleep()
                return

    def hangDetector(self):
        '''
        Detect client disconnection while in the middle of something
        or just normal connection close.
        '''
        logger = logging.getLogger('http_hangDetector')
        logger.debug("Started")
        try:
            while True:
                if not self.rfile.read():
                    break
        except:
            pass
        finally:
            self.clientconnected = False
            logger.debug("Client disconnected")
            try:
                self.requestgreenlet.kill()
                self.proxyReadWritegreenlet.kill()
                gevent.sleep()
            except:
                pass
            return

    def do_GET(self):
        '''
        GET request handler
        '''
        logger = logging.getLogger('http_HTTPHandler')
        self.clientconnected = True
        # Don't wait videodestroydelay if error happened
        self.errorhappened = True
        # Headers sent flag for fake headers UAs
        self.headerssent = False
        # Current greenlet
        self.requestgreenlet = gevent.getcurrent()
        # Connected client IP address
        self.clientip = self.request.getpeername()[0]

        logger.info("Accepted connection from " + self.clientip + " path " + self.path)

        try:
            self.splittedpath = self.path.split('/')
            self.reqtype = self.splittedpath[1].lower()
            # If first parameter is 'pid' or 'torrent' or it should be handled
            # by plugin
            if not (self.reqtype in ('pid', 'torrent') or self.reqtype in AceStuff.pluginshandlers):
                self.dieWithError(400)  # 400 Bad Request
                return
        except IndexError:
            self.dieWithError(400)  # 400 Bad Request
            return

        # Handle request with plugin handler
        if self.reqtype in AceStuff.pluginshandlers:
            try:
                AceStuff.pluginshandlers.get(self.reqtype).handle(self)
            except Exception as e:
                logger.error('Plugin exception: ' + repr(e))
                self.dieWithError()
            finally:
                self.closeConnection()
                return

        # Check if third parameter exists
        # …/pid/blablablablabla/video.mpg
        #                      |_________|
        # And if it ends with regular video extension
        try:
            if not self.path.endswith(('.3gp', '.avi', '.flv', '.mkv', '.mov', '.mp4', '.mpeg', '.mpg', '.ogv', '.ts')):
                logger.error("Request seems like valid but no valid video extension was provided")
                self.dieWithError(400)
                return
        except IndexError:
            self.dieWithError(400)  # 400 Bad Request
            return

        # Limit concurrent connections
        if AceConfig.maxconns > 0 and AceStuff.clientcounter.total >= AceConfig.maxconns:
            logger.debug("Maximum connections reached, can't serve this")
            self.dieWithError(503)  # 503 Service Unavailable
            return

        # Pretend to work fine with Fake UAs
        if self.headers.get('User-Agent') and self.headers.get('User-Agent') in AceConfig.fakeuas:
            logger.debug("Got fake UA: " + self.headers.get('User-Agent'))
            # Return 200 and exit
            self.send_response(200)
            self.send_header("Content-Type", "video/mpeg")
            self.end_headers()
            self.closeConnection()
            return

        self.path_unquoted = urllib2.unquote(self.splittedpath[2])
        # Make list with parameters
        self.params = list()
        for i in xrange(3, 8):
            try:
                self.params.append(int(self.splittedpath[i]))
            except (IndexError, ValueError):
                self.params.append('0')

        # Adding client to clientcounter
        clients = AceStuff.clientcounter.add(self.path_unquoted, self.clientip)
        # If we are the one client, but sucessfully got ace from clientcounter,
        # then somebody is waiting in the videodestroydelay state
        self.ace = AceStuff.clientcounter.getAce(self.path_unquoted)
        if not self.ace:
            shouldcreateace = True
        else:
            shouldcreateace = False

        # Use PID as VLC ID if PID requested
        # Or torrent url MD5 hash if torrent requested
        if self.reqtype == 'pid':
            self.vlcid = self.path_unquoted
        else:
            self.vlcid = hashlib.md5(self.path_unquoted).hexdigest()

        # If we don't use VLC and we're not the first client
        if clients != 1 and not AceConfig.vlcuse:
            AceStuff.clientcounter.delete(self.path_unquoted, self.clientip)
            logger.error(
                "Not the first client, cannot continue in non-VLC mode")
            self.dieWithError(503)  # 503 Service Unavailable
            return

        if shouldcreateace:
        # If we are the only client, create AceClient
            try:
                self.ace = aceclient.AceClient(
                    AceConfig.acehost, AceConfig.aceport, connect_timeout=AceConfig.aceconntimeout,
                    result_timeout=AceConfig.aceresulttimeout)
                # Adding AceClient instance to pool
                AceStuff.clientcounter.addAce(self.path_unquoted, self.ace)
                logger.debug("AceClient created")
            except aceclient.AceException as e:
                logger.error("AceClient create exception: " + repr(e))
                AceStuff.clientcounter.delete(
                    self.path_unquoted, self.clientip)
                self.dieWithError(502)  # 502 Bad Gateway
                return

        # Send fake headers if this User-Agent is in fakeheaderuas tuple
        if self.headers.get('User-Agent') and self.headers.get('User-Agent') in AceConfig.fakeheaderuas:
            logger.debug(
                "Sending fake headers for " + self.headers.get('User-Agent'))
            self.send_response(200)
            self.send_header("Content-Type", "video/mpeg")
            self.end_headers()
            # Do not send real headers at all
            self.headerssent = True

        try:
            self.hanggreenlet = gevent.spawn(self.hangDetector)
            logger.debug("hangDetector spawned")
            gevent.sleep()

            # Initializing AceClient
            if shouldcreateace:
                self.ace.aceInit(
                    gender=AceConfig.acesex, age=AceConfig.aceage,
                    product_key=AceConfig.acekey, pause_delay=AceConfig.videopausedelay)
                logger.debug("AceClient inited")
                if self.reqtype == 'pid':
                    self.ace.START(
                        self.reqtype, {'content_id': self.path_unquoted, 'file_indexes': self.params[0]})
                elif self.reqtype == 'torrent':
                    self.paramsdict = dict(
                        zip(aceclient.acemessages.AceConst.START_TORRENT, self.params))
                    self.paramsdict['url'] = self.path_unquoted
                    self.ace.START(self.reqtype, self.paramsdict)
                logger.debug("START done")

            # Getting URL
            self.url = self.ace.getUrl(AceConfig.videotimeout)
            # Rewriting host for remote Ace Stream Engine
            self.url = self.url.replace('127.0.0.1', AceConfig.acehost)
            self.errorhappened = False

            if shouldcreateace:
                logger.debug("Got url " + self.url)
                # If using VLC, add this url to VLC
                if AceConfig.vlcuse:
                    # Force ffmpeg demuxing if set in config
                    if AceConfig.vlcforceffmpeg:
                        self.vlcprefix = 'http/ffmpeg://'
                    else:
                        self.vlcprefix = ''

                    # Sleeping videodelay
                    gevent.sleep(AceConfig.videodelay)

                    AceStuff.vlcclient.startBroadcast(
                        self.vlcid, self.vlcprefix + self.url, AceConfig.vlcmux, AceConfig.vlcpreaccess)
                    # Sleep a bit, because sometimes VLC doesn't open port in
                    # time
                    gevent.sleep(0.5)

            # Building new VLC url
            if AceConfig.vlcuse:
                self.url = 'http://' + AceConfig.vlchost + \
                    ':' + str(AceConfig.vlcoutport) + '/' + self.vlcid
                logger.debug("VLC url " + self.url)

            # Sending client headers to videostream
            self.video = urllib2.Request(self.url)
            for key in self.headers.dict:
                self.video.add_header(key, self.headers.dict[key])

            self.video = urllib2.urlopen(self.video)

            # Sending videostream headers to client
            if not self.headerssent:
                self.send_response(self.video.getcode())
                if self.video.info().dict.has_key('connection'):
                    del self.video.info().dict['connection']
                if self.video.info().dict.has_key('server'):
                    del self.video.info().dict['server']
                if self.video.info().dict.has_key('transfer-encoding'):
                    del self.video.info().dict['transfer-encoding']
                if self.video.info().dict.has_key('keep-alive'):
                    del self.video.info().dict['keep-alive']

                for key in self.video.info().dict:
                    self.send_header(key, self.video.info().dict[key])
                # End headers. Next goes video data
                self.end_headers()
                logger.debug("Headers sent")

            if not AceConfig.vlcuse:
                # Sleeping videodelay
                gevent.sleep(AceConfig.videodelay)

            # Spawning proxyReadWrite greenlet
            self.proxyReadWritegreenlet = gevent.spawn(self.proxyReadWrite)

            # Waiting until all greenlets are joined
            gevent.joinall((self.proxyReadWritegreenlet, self.hanggreenlet))
            logger.debug("Greenlets joined")

        except (aceclient.AceException, vlcclient.VlcException, urllib2.URLError) as e:
            logger.error("Exception: " + repr(e))
            self.errorhappened = True
            self.dieWithError()
        except gevent.GreenletExit:
            # hangDetector told us about client disconnection
            pass
        except Exception as e:
            # Unknown exception
            logger.error("Unknown exception: " + repr(e))
            self.errorhappened = True
            self.dieWithError()
        finally:
            logger.debug("END REQUEST")
            AceStuff.clientcounter.delete(self.path_unquoted, self.clientip)
            if not self.errorhappened and not AceStuff.clientcounter.get(self.path_unquoted):
                # If no error happened and we are the only client
                logger.debug("Sleeping for " + str(
                    AceConfig.videodestroydelay) + " seconds")
                gevent.sleep(AceConfig.videodestroydelay)
            if not AceStuff.clientcounter.get(self.path_unquoted):
                logger.debug("That was the last client, destroying AceClient")
                if AceConfig.vlcuse:
                    try:
                        AceStuff.vlcclient.stopBroadcast(self.vlcid)
                    except:
                        pass
                self.ace.destroy()
                AceStuff.clientcounter.deleteAce(self.path_unquoted)


class HTTPServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):

    def handle_error(self, request, client_address):
        # Do not print HTTP tracebacks
        try:
            pass
        except Exception as e:
            print repr(e)


class AceStuff(object):
    pass


logging.basicConfig(
    filename=AceConfig.logpath + 'acehttp.log' if AceConfig.loggingtoafile else None,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s', datefmt='%d.%m.%Y %H:%M:%S', level=AceConfig.debug)
logger = logging.getLogger('INIT')

# Loading plugins
os.chdir(os.path.dirname(os.path.realpath(__file__)))
# Creating dict of handlers
AceStuff.pluginshandlers = dict()
# And a list with plugin instances
AceStuff.pluginlist = list()
pluginsmatch = glob.glob('plugins/*_plugin.py')
sys.path.insert(0, 'plugins')
pluginslist = [os.path.splitext(os.path.basename(x))[0] for x in pluginsmatch]
for i in pluginslist:
    plugin = __import__(i)
    plugname = i.split('_')[0].capitalize()
    try:
        plugininstance = getattr(plugin, plugname)(AceConfig, AceStuff)
    except Exception as e:
        logger.error("Cannot load plugin " + plugname + ": " + repr(e))
        continue
    logger.debug('Plugin loaded: ' + plugname)
    for j in plugininstance.handlers:
        AceStuff.pluginshandlers[j] = plugininstance
    AceStuff.pluginlist.append(plugininstance)

server = HTTPServer((AceConfig.httphost, AceConfig.httpport), HTTPHandler)
logger = logging.getLogger('HTTP')

# Creating ClientCounter
AceStuff.clientcounter = ClientCounter()

if AceConfig.vlcuse:
    # Creating VLC VLM Client
    try:
        AceStuff.vlcclient = vlcclient.VlcClient(
            host=AceConfig.vlchost, port=AceConfig.vlcport, password=AceConfig.vlcpass,
            out_port=AceConfig.vlcoutport)
    except vlcclient.VlcException as e:
        print repr(e)
        quit()


try:
    logger.info("Server started.")
    server.serve_forever()
except KeyboardInterrupt:
    logger.info("Stopping server...")
    server.shutdown()
    server.server_close()
    for i in AceStuff.pluginlist:
        del i
