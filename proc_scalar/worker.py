import os
import irc
import time
import math
import heroku
import requests
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from schema import App
from pprint import pprint # noqa

DATABASE_URL = os.environ.get('DATABASE_URL', False)
SLEEP_PERIOD = float(os.environ.get('SLEEP_PERIOD', 10))
HEROKU_API_KEY = os.environ.get('HEROKU_API_KEY', False)
NOTIFICATIONS = os.environ.get('NOTIFICATIONS', False)

max_str_length = 180

assert(DATABASE_URL)
assert(HEROKU_API_KEY)

print "[INIT] using DATABASE_URL=%s" % DATABASE_URL
print "[INIT] using SLEEP_PERIOD=%s" % SLEEP_PERIOD
print "[INIT] using HEROKU_API_KEY=%s" % HEROKU_API_KEY


def process_apps(app, heroku_conn, heroku_app):
    data = get_data(app)
    if not data:
        return

    heroku_dynos = heroku_app.dynos()
    heroku_procs = heroku_app.process_formation()

    for procname in data.iterkeys():
        count = data[procname]['count']
        active_count = data[procname]['active']
        if not procname in heroku_procs:
            print "[%s] %s not found in heroku proc formation, skipping".ljust(max_str_length) % (app.appname, procname)
            continue

        print heroku_procs
        heroku_proc = heroku_procs[procname]
        print heroku_proc
        if 'deploy_lock' in data[procname] and data[procname]['deploy_lock'] != 0:
            print "[%s] %s is locked for %s, skipping".ljust(max_str_length) % (app.appname, procname, data[procname]['deploy_lock'])
            continue

        print "[%s] Checking for scaling on %s".ljust(max_str_length) % (app.appname, procname)
        check_for_scaling(heroku_conn, heroku_app, app, heroku_dynos, heroku_proc, count, active_count)


def scale_dyno(heroku_conn, heroku_app, heroku_dynos, heroku_proc, count):

    if count == 0:
        # we need to call the shutdown control_app
        shutdown_app(heroku_conn, heroku_app, heroku_dynos, heroku_proc)
    else:
        if NOTIFICATIONS:
            irc.send_irc_message("[%s] Scaling %s processes to %s" % (heroku_app.name, heroku_proc.type, count))
        heroku_proc.scale(count)


def shutdown_app(heroku_conn, heroku_app, heroku_dynos, heroku_proc):

    running_already = 0
    cmd = "fab shutdown_celery_process:%s" % heroku_proc.type

    for dyno in heroku_dynos:
        if dyno.command == cmd:
            running_already = 1
            break

    if running_already == 1:
        print "[%s] Shutdown command for %s already running... skipping....".ljust(max_str_length) % (heroku_app.name, heroku_proc.type)
    else:
        print "[%s] shutting down processes %s".ljust(max_str_length) % (heroku_app.name, heroku_proc.type)
        if NOTIFICATIONS:
            irc.send_irc_message("[%s] shutting down processes %s" % (heroku_app.name, heroku_proc.type))
        heroku_app.run_command_detached(cmd)


def get_current_dynos(app, heroku_dynos, heroku_proc):

    cpt = 0
    print heroku_dynos
    if heroku_proc.type in heroku_dynos:
        print "################"
        for dyno in heroku_dynos[heroku_proc.type]:
            print "checking dyno {0}".format(dyno.name)
            if dyno.state == 'crashed':
                #check how long ago and scale it down if it crashed
                print "[{0}] {1} is crashed - Killing it".format(app.appname, heroku_proc.type)
                dyno.kill()
            else:
                cpt += 1

    return cpt


def check_for_scaling(heroku_conn, heroku_app, app, heroku_dynos, heroku_proc, count, active_tasks):
    appname = app.appname
    max_dynos = int(app.max_dynos)
    min_dynos = int(app.min_dynos)

    required_count = calculate_required_dynos(count, max_dynos, min_dynos, int(app.count_boundary))
    #current_dyno_count = heroku_proc.quantity
    #can't do this because if the dyno is 'crashed' it will still show up in heroku_proc.quantity!!
    current_dyno_count = int(get_current_dynos(app, heroku_dynos, heroku_proc))
    print "[%s] %s has %s running dynos and %s pending tasks".ljust(max_str_length) % (appname, heroku_proc.type, current_dyno_count, count)

    if not current_dyno_count == required_count:
        print "[%s] Scaling %s dyno process to %d".ljust(max_str_length) % (appname, heroku_proc.type, required_count)
        if required_count == 0 and active_tasks > 0:
            print "[%s] Not shutting down %s dyno yet as it still has %s active tasks".ljust(max_str_length) % (appname, heroku_proc.type, active_tasks)
        else:
            scale_dyno(heroku_conn, heroku_app, heroku_dynos, heroku_proc, required_count)


def calculate_required_dynos(count, max_dynos, min_dynos, count_boundary):

    if count_boundary == 0:
        if count > 0:
            return 1
        else:
            if count <= min_dynos:
                # print "Min dynos reached"
                return min_dynos
            else:
                return 0
    else:
        if count > 0:
            calc = math.ceil(float(count) / float(count_boundary))
            if calc >= max_dynos:
                # print "Max dynos reached"
                return max_dynos
            else:
                return calc
        else:
            if count <= min_dynos:
                # print "Min dynos reached"
                return min_dynos
            else:
                return 0


def get_data(app):
    if app.username or app.password:
        # print "[%s]Loading data, using authentication method please wait..... %s ".ljust(max_str_length) % (app.appname, app.app_api_url)
        try:
            r = requests.get(app.app_api_url, auth=(app.username, app.password), timeout=10.0)
        except Exception, e:
            print "\n[Error] %s for %s" % (e, app.app_api_url)
            return

        if not r.status_code == 200:
            print "\n[ERROR] %s call to %s with user = %s and password = %s Returned response code %s and the following message" % (
                    app.appname, app.app_api_url, app.username, app.password, r.status_code)
            #print r.text
            return
    else:
        # print "[%s]Loading data, please wait.....".ljust(max_str_length) % app.appname
        try:
            r = requests.get(app.app_api_url)
        except Exception, e:
            print "\n[Error] %s for %s" % (e, app.app_api_url)
            return
        if not r.status_code == 200:
            print "[ERROR] %s call to %s without user or password Returned response code %s and the following message" % (
                    app.appname, app.app_api_url, r.status_code)
            return

    return r.json


engine = create_engine(DATABASE_URL)
Session = scoped_session(sessionmaker(bind=engine))
while(True):
    print "\n\n====================[Beginning Run]=======================\n".ljust(max_str_length)
    session = Session()
    apps = session.query(App).order_by("app_appname").all()
    my_config = {'verbose': sys.stderr}
    session = requests.session(config=my_config)
    heroku_conn = heroku.from_key(HEROKU_API_KEY, session=session)
    print "t0"
    print("rate_limit_remaining = {0}".format(heroku_conn.ratelimit_remaining()))
    print "t1"
    heroku_apps = heroku_conn.apps()
    print "t2"
    for app in apps:
        try:
            heroku_app = heroku_apps[app.appname]
        except KeyError:
            print "\n[ERROR] %s is not available via your configured HEROKU_API %s.\nAvailable apps are:-\n" % (app.appname, HEROKU_API_KEY)
        else:
            process_apps(app, heroku_conn, heroku_app)
            time.sleep(3)
    print "Cycle Complete sleeping for %f".ljust(max_str_length) % SLEEP_PERIOD
    time.sleep(SLEEP_PERIOD)
