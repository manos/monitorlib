#
# Author: Charlie Schluting <charlie@krux.com>
#
"""
 Library to make outputting notifications and values in collectd plugins easier.
 Handles notifications itself; supports email, pagerduty, arbitrary URL to POST
 JSON to (or even raw TCP sending of JSON).

 = Usage:
 See: examples/collectd_check.py

 Within your own collectd plugin, simply:
 import monitorlib.collectd as collectd

 Then, you can use collectd.[warning|ok|failure]("message content", [options]).

 likewise, you can use it to output values:
 collectd.metric("plugin-instance/type-instance", value)

 = Interface docs:

  == ok("status message", [page=True], [email='A@host,B@host'], [url])
  == warning("status message", [page=False], [email='A@host,B@host'], [url])
  == failure("status message", [page=True], [email='A@host,B@host'], [url])

  Arguments:
  message: text of the alert
  page: set to true to initiate sending to pagerduty. Must have called
        set_pagerduty_key() first.
  email: one or more comma-separated emails to send to: 'user@host,user2@host'
  url: URL to HTTP POST the JSON alert to

  == optional configuration (required to enable some options):
    === set_pagerduty_key("12309423enfjsdjfosiejfoiw") to set pagerduty auth
    === set_pagerduty_store([file|redis], [path])
        Location to store state information on outstanding alerts.
        To use redis: call set_redis_config() (see below)
        Default is: set_pagerduty_store('file', '/tmp/incident_keys')
    === set_redis_config(writer_host, reader_host, writer_port, reader_port, password, [db])
        to enable checking with redis for disabled alerts, and pagerduty incident_keys.

  == metric("testing/records", int)

  Arguments:
  metric: string of collectd metric (excluding host, it's added automatically) -
          make sure it's formatted as collectd expects, or it'll be dropped!
          (make sure last item is in /usr/share/collectd/types.db, to start)
          Krux-specific: always use the counter type, and it'll be removed by graphite.
          To get stats.$env.ops.collectd.$host.plugin.instance.foo, use "plugin-instance/counter-foo".
  value: integer value

 = Thoughts:
  You can use this to send to pagerduty or elsewhere directly through your service
  check plugins. But, that's old-school nagios style.
  Ideally, you'll simply wrap this library to set the defaults to False for everything
  but the URL argument, which will cause this lib to POST JSON every time the check
  runs. This should go to a decision engine (like riemann, for example), where you
  can verify the state of other checks (LB status, cluster health, parent relationships,
  etc) before alerting (or even displaying a status) for real.

"""
import time
import socket
import os
import sys
import subprocess
import inspect
import logging
import urllib2
import smtplib
from optparse import OptionParser
import monitorlib.pagerduty as pagerduty
from email.MIMEMultipart import MIMEMultipart
from email.MIMEText import MIMEText
try:
    import simplejson as json
except ImportError:
    import json

try:
    import redis
except ImportError:
    pass

FQDN = os.environ.get('COLLECTD_HOSTNAME', socket.gethostname())
INTERVAL = os.environ.get('COLLECTD_INTERVAL', "60")
#CALLER = os.path.basename(inspect.stack()[-1][1])
CALLER = os.path.basename(sys.argv[0])
TIME = int(time.mktime(time.gmtime()))
DATASTORE = None

def set_datastore(store):
    global DATASTORE
    DATASTORE = store

def set_pagerduty_key(key):
    global PD_KEY
    PD_KEY = key

def set_redis_config(writer_host, reader_host, writer_port, reader_port, password, db='db0'):
    global REDIS_CONFIG
    REDIS_CONFIG = { 'writer': writer_host,
                     'reader': reader_host,
                     'writer_port': writer_port,
                     'reader_port': reader_port,
                     'passwd': password,
                     'db': db,
                   }
    set_datastore('redis')

def set_state_dir(dir="/tmp"):
    global STATE_DIR
    STATE_DIR = dir

def send_to_socket(message, host, port):
    """
    Sends message to host/port via tcp
    """
    sock = socket.socket()

    sock.connect((host, int(port)))
    sock.sendall(message)
    sock.close()

def post_to_url(message, url):
    """
    HTTP POSTs message to url
    """
    req = urllib2.Request(url, json.dumps(message), {'Content-Type': 'application/json'})
    f = urllib2.urlopen(req)
    resp = f.read()
    f.close()

    return resp

def set_pagerduty_store(kind='file', config='/tmp/incident_keys'):
    """
    sets PD storage method, and stores a variable to indicate this has been done
    """
    global PD_STORAGE_CONFIGURED
    if pagerduty.set_datastore(kind, config):
        PD_STORAGE_CONFIGURED = True

def send_to_pagerduty(key, message):
    """
    Sends alert to pager duty - you must call authenticate() first
    """
    # if not already done, call config function to set defaults
    if 'PD_STORAGE_CONFIGURED' not in globals():
        if 'REDIS_CONFIG' in globals():
            set_pagerduty_store('redis', REDIS_CONFIG)
        else:
            if 'STATE_DIR' in globals():
                set_pagerduty_store('file', STATE_DIR.lstrip('/') + "/incident_keys")

    pagerduty.authenticate(key)

    send_string = "%s %s: %s %s" % (message['host'], message['plugin'], message['severity'].upper(), message['message'])

    if 'okay' in message['severity']:
        pagerduty.event('resolve', send_string)

    elif 'failure' or 'warning' in message['serverity']:
        pagerduty.event('trigger', send_string)

def send_to_email(address, message):
    """
    Sends alert via email
    """
    print "emailing: ", address

    alert_subject = "%s %s: %s" % (message['host'], message['plugin'], message['message'])

    me = 'collectd@krux.com'
    you = str(address)

    msg = MIMEMultipart()
    msg['Subject'] = '[collectd] %s %s' % (message['severity'].upper(), alert_subject)
    msg['From'] = me
    msg['To'] = you
    body = MIMEText(str(message))
    msg.attach(body)

    s = smtplib.SMTP('localhost')
    s.sendmail(me, [you], msg.as_string())
    s.quit()

def cmd(command):
    """
    Helper for running shell commands with subprocess(). Returns:
    (stdout, stderr)
    """
    process = subprocess.Popen(command, shell=True, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
    return process.communicate()

def check_redis_alerts_disabled(message):
    """
    Check redis to see if alerts are disabled for this host - times out after 2 seconds,
    to not block on an unreachable redis server.
    """
    conf = REDIS_CONFIG

    # key: host, value: list of plugins that are disabled (or '*' for all)
    conn = redis.Redis(conf['reader'], conf['reader_port'], conf['db'], conf['passwd'], socket_timeout=2)

    if not conn:
        return False
    else:
        global_acks = conn.get('global')
        if global_acks and ('*' in global_acks or message['plugin'] in global_acks):
            return True
        else:
            result = conn.get(message['host'])

    if result and ('*' in result or message['plugin'] in result):
        return True
    else:
        return False

def dispatch_alert(severity, message, page, email, url):
    """
    dispatch_alertes alerts based on params, and keep state, etc...
    """

    message = json.loads('{"host": "%s", "plugin": "%s", "severity": "%s", "message": "%s"}' % (FQDN.split('.')[0], CALLER, severity, message))

    # check if notifications for this host are disabled, and bail if so
    if DATASTORE and 'redis' in DATASTORE:
        if 'REDIS_CONFIG' not in globals():
            logging.error("must call redis_config(), first")
        elif check_redis_alerts_disabled(message):
            logging.info("alerting disabled, supressing alert for: %s, %s" % (message['host'], message['plugin']))
            return None

    if 'STATE_DIR' not in globals():
        set_state_dir()

    # get last_state:
    state = 'new'
    state_file = STATE_DIR + "/%s" % message['plugin']

    if not os.path.exists(STATE_DIR) or not os.access(STATE_DIR, os.W_OK):
        logging.error("state_dir: no such file or directory, or unwritable")
        return None

    if os.path.exists(state_file):
        if not os.access(state_file, os.W_OK):
            # try to chown it? hehe
            cmd("sudo chown %s %s" % (os.getenv('USER'), state_file))

        with open(state_file, 'r') as fh:
            prev_state = fh.readline()
        if prev_state not in message['severity']:
            # doesn't match? the state changed.
            state = 'transitioned'
    else:
        # state file didn't exist - first-run of this check, don't alert.
        state = 'new'

    # write the current state:
    with open(state_file, 'w') as fh:
        fh.write(message['severity'])

    # if paging was requested, do it, unless the state is the same as last time
    if page and 'transitioned' in state:
        if 'PD_KEY' not in globals():
            logging.error("must call set_pagerduty_key(), first")
        else:
            send_to_pagerduty(PD_KEY, message)

    # only email if state is new since last time
    if email and 'transitioned' in state:
        send_to_email(email, message)

    # if 'url' was requested, always post to it regardless of state
    if url:
        post_to_url(message, url)



def failure(string, page=False, email=False, url=False):
    return dispatch_alert('failure', string, page, email, url)

def warning(string, page=False, email=False, url=False):
    return dispatch_alert('warning', string, page, email, url)

def ok(string, page=False, email=False, url=False):
    return dispatch_alert('okay', string, page, email, url)

def metric(path, value):
    ''' formats and returns a collectd metric value (str) '''
    return "PUTVAL %s/%s interval=%s N:%s" % (FQDN, path, INTERVAL, value)


