import os
import io
import re
import sys
import pdb
import time
import shutil
import pickle
import logging
import logging.config

from random import randrange
from getpass import getpass
from os.path import abspath, dirname, join, isfile, isdir
import datetime
from pytz import timezone

import json
import requests
import lxml.html
from selenium import webdriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import NoSuchElementException
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options 
from selenium.webdriver.common.proxy import *
from pymongo import MongoClient
from PIL import Image
import piexif
from google.cloud import storage

# -----------------------------------------------------------------------------
# Logging stuff
# -----------------------------------------------------------------------------
LOGGING_CONFIG = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {
            'format': "%(asctime)s %(levelname)s %(module)s::%(funcName)s: %(message)s",
            'datefmt': '%H:%M:%S'
        }
    },
    'handlers': {
        'app': {'level': 'DEBUG',
                    'class': 'ansistrm.ColorizingStreamHandler',
                    'formatter': 'standard'},
        'default': {'level': 'ERROR',
                    'class': 'ansistrm.ColorizingStreamHandler',
                    'formatter': 'standard'},
    },
    'loggers': {
        'default': {
            'handlers': ['default'], 'level': 'ERROR', 'propagate': False
        },
         'app': {
            'handlers': ['app'], 'level': 'DEBUG', 'propagate': True
        },

    },
}
logging.config.dictConfig(LOGGING_CONFIG)


# -----------------------------------------------------------------------------
# The scraper code.
# -----------------------------------------------------------------------------
class DownloadError(Exception):
    pass


class Client:
    MONGO_URL = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/test_db')
    COOKIE_FILE = "state/cookies.pkl"
    TIMESTAMP_FILE = "state/timestamp"
    ROOT_URL = "https://www.tadpoles.com/"
    HOME_URL = "https://www.tadpoles.com/parents"
    LIST_BASE_URL = ROOT_URL+"remote/v1/events?direction=range"
    MIN_SLEEP = 1
    MAX_SLEEP = 3
    NUM_DAYS = int(os.getenv("NUM_DAYS","45"))
    DAY_RANGE = datetime.timedelta(days=NUM_DAYS)
    BUCKET_NAME = os.getenv("AWS_BUCKET_NAME","tadpoles")
    GS_CLIENT = storage.Client()
    BUCKET = GS_CLIENT.get_bucket(BUCKET_NAME)

    def __init__(self):
        self.init_logging()

    def init_logging(self):
        logger = logging.getLogger('app')
        self.info = logger.info
        self.debug = logger.debug
        self.warning = logger.warning
        self.critical = logger.critical
        self.exception = logger.exception

    def __enter__(self):
        options = Options()
        #google="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/85.0.4183.102 Safari/537.36"
        #google="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36"
        #google='Mozilla/5.0 (Macintosh; Intel Mac OS X 10.11; rv:46.0) Gecko/20100101 Firefox/46.0'

        #options.add_argument(f'user-agent={google}')
        options.add_experimental_option("useAutomationExtension", False)
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_argument("no-default-browser-check")
        options.add_argument("no-first-run")
        options.add_argument("no-sandbox")
        options.add_argument("disable-gpu")
        options.add_argument("disable-dev-shm-usage")
        options.add_argument("disable-setuid-sandbox")
        options.headless = True
        options.binary_location = "/app/.apt/usr/bin/google-chrome"

        self.info("Starting browser")
        self.br = self.browser = webdriver.Chrome(executable_path="/app/.chromedriver/bin/chromedriver",chrome_options=options) 
        self.br.implicitly_wait(10)
        return self

    def __exit__(self, *args):
        self.info("Shutting down browser")
        self.browser.quit()

    def sleep(self, minsleep=None, maxsleep=None):
        _min = minsleep or self.MIN_SLEEP
        _max = maxsleep or self.MAX_SLEEP
        duration = randrange(_min * 100, _max * 100) / 100.0
        self.debug('Sleeping %r' % duration)
        time.sleep(duration)

    def navigate_url(self, url):
        self.info("Navigating to %r", url)
        self.br.get(url)

    def dump_to_db(self, item_type, data):
        client = MongoClient(self.MONGO_URL)
        try:
            db = client.get_default_database().settings
            db.replace_one({'type':item_type},{'type': item_type, 'value': data},True)
        except Exception as exc:
            self.exception(exc)

    def load_from_db(self, item_type):
        client = MongoClient(self.MONGO_URL)
        try:
            db = client.get_default_database().settings
            value = db.find_one({'type':item_type})
            if value is not None:
                return (db.find_one({'type':item_type}))['value']
        except Exception as exc:
            self.exception(exc)
        return None

    def check_cookie_valid(self):
        self.requestify_cookies()
        try:
            cookies=[]
            resp = requests.get(self.HOME_URL,cookies=self.req_cookies, allow_redirects=False)
            if resp.status_code == 200:
                return True
        except:
            msg = 'Error (%r) validating cookie %r'
            raise DownloadError(msg % (resp.status_code, self.HOME_URL))
        return False
    
    def load_cookies_db(self):
        self.info("Loading cookies from db.")
        self.cookies = self.load_from_db('cookie')
        if self.cookies is None:
            raise FileNotFoundError ("cookie not found in db")
        self.cookies = pickle.loads(self.cookies)
        if not self.check_cookie_valid():
            raise FileNotFoundError ("cookie was invalid")

    def dump_cookies_db(self):
        self.info("Dumping cookies to db.")
        self.dump_to_db ('cookie', pickle.dumps(self.br.get_cookies()))

    def dump_screenshot_db(self):
        self.info("Dumping screenshot to db.")
        self.dump_to_db ('screenshot', self.br.get_screenshot_as_png())

    def add_cookies_to_browser(self):
        self.info("Adding the cookies to the browser.")
        for cookie in self.cookies:
            if self.br.current_url.strip('/').endswith(cookie['domain']):
                self.br.add_cookie(cookie)

    def requestify_cookies(self):
        # Cookies in the form reqeusts expects.
        self.info("Transforming the cookies for requests lib.")
        self.req_cookies = {}
        for s_cookie in self.cookies:
            self.req_cookies[s_cookie["name"]] = s_cookie["value"]

    def switch_windows(self, pageTitle):
        '''Switch to the other window.'''
        self.info("Switching windows.")
        all_windows = set(self.br.window_handles)
        self.info(all_windows)

        try:
            for win in all_windows:
                self.br.switch_to.window(win)
                self.sleep()
                self.info(self.br.title)
                if self.br.title.startswith(pageTitle): return
            #current_window = set([self.br.current_window_handle])
            #self.info(self.br.title)
            #self.info(current_window)
            #other_window = (all_windows - current_window).pop()
            #self.info(other_window)
            #self.br.switch_to.window(other_window)
            self.sleep()
            self.info(self.br.title)
        except Exception as exc:
            self.exception(exc)
            self.info("window not found")
            current_window = self.br.window_handles[0]
            self.br.switch_to.window(current_window)
            self.sleep()
            self.info(self.br.title)
            
        self.info(current_window)

    def dump_timestamp_db(self, timestamp):
        self.info("Dumping Timestamp to db.")
        self.dump_to_db ('timestamp', pickle.dumps(timestamp))

    def load_timestamp_db(self):
        self.info("Loading Timestamp from db.")
        self.full_sync = False
        start_time = self.load_from_db('timestamp')
        if start_time is None:
            self.debug("Timestamp was empty")
            start_time = datetime.datetime.now()
            self.full_sync = True
        else:
            start_time = pickle.loads(start_time)
        return start_time
        
    def get_api(self):
        end_time = datetime.datetime.now()
        start_time = self.load_timestamp_db()
        self.dump_timestamp_db(end_time)   

        while True:
            if self.full_sync:
                start_time=end_time-self.DAY_RANGE

            start_time_val=int(time.mktime(start_time.timetuple()))

            start_string="&earliest_event_time="+str(start_time_val)
            end_string="&latest_event_time="+str(int(time.mktime(end_time.timetuple())))

            num_events="&num_events=300&client=dashboard"

            url = self.LIST_BASE_URL+start_string+end_string+num_events
            self.info(url)
            try:
                coolCookie = os.getenv("COOKIE")
                email=os.getenv("EMAIL")
                HEADERS = {
                    'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:69.0) Gecko/20100101 Firefox/69.0',
                    'Accept': 'application/json, text/javascript, */*; q=0.01',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'X-TADPOLES-UID': email,
                    'X-Requested-With': 'XMLHttpRequest',
                    'Connection': 'keep-alive',
                    'Referer': 'https://www.tadpoles.com/parents',
                    'cookie': coolCookie
                }

                resp = requests.get(url, headers=HEADERS)
                if resp.status_code != 200:
                    msg = 'Error (%r) downloading %r'
                    raise DownloadError(msg % (resp.status_code, url))

                jsonData = json.loads(resp.text)

                if len(jsonData['events']) == 0:
                    break
                for event in jsonData['events']:
                    if len(event['attachments']) > 0:
                        for attachment in event['new_attachments']:
                            self.save_image_api(attachment['key'],event['event_time'],attachment['mime_type'])
                
                if not self.full_sync:
                    break
                end_time=start_time
            except Exception as exc:
               self.exception(exc)
               self.dump_timestamp_db(start_time)
               return
               
    def do_login(self):
        # Navigate to login page.
        self.info("Navigating to login page.")
        #self.br.find_element_by_id("login-button").click()
        self.br.find_element_by_xpath("//div[@data-bind = 'click: chooseParent']").click()
        self.br.find_element_by_xpath("//img[@data-bind = 'click:loginGoogle']").click()

        # Focus on the google auth popup.
        self.switch_windows("Sign in - Google Accounts")
        try:
            WebDriverWait(self.br, 10).until(EC.visibility_of_element_located((By.ID, "identifierId")))
            # Enter email.
            email = self.br.find_element_by_id("identifierId")
            email.send_keys(input("Enter email: "))
            self.br.find_element_by_id("identifierNext").click()
            self.sleep()
    
            # Enter password.
            passwd = self.br.find_element_by_css_selector("input[type='password'][name='password']")
            passwd.send_keys(getpass("Enter password:"))
            self.br.find_element_by_id("passwordNext").click()
            
            # Enter 2FA pin.
            #Epin = self.br.find_element_by_id("idvPreregisteredPhonePin")
            #pin.send_keys(getpass("Enter google verification code: "))
            #pin.submit()
        except Exception as exc:
            email = self.br.find_element_by_id("Email")
            email.send_keys(input("Enter email: "))
            self.sleep()

            self.br.find_element_by_id("next").click()
            self.sleep()
            self.br.save_screenshot("state/after_login.png")
            self.dump_screenshot_db()

            WebDriverWait(self.br, 10).until(EC.visibility_of_element_located((By.ID, "Passwd")))
            passwd = self.br.find_element_by_id("Passwd")
            passwd.send_keys(getpass("Enter password:"))
            self.sleep()

            self.br.find_element_by_id("signIn").click()
            self.exception(exc)

        self.sleep()
        self.br.save_screenshot("state/after_login.png")
        self.dump_screenshot_db()
        # Click "approve".
        self.info("Sleeping 2 seconds.")
        self.sleep(minsleep=10,maxsleep=15)
        self.info("Clicking 'approve' button.")
        #self.br.find_element_by_id("submit_approve_access").click()
        
        # Switch back to tadpoles.
        self.switch_windows("Simplifying Childcare")
        
    def write_exif(self, response, timestamp):
        response.raw.decode_content = True
        exif_dict = {}
        exif_ifd = {}
        zeroth_ifd = {}
        try:
            image = Image.open(response.raw)
            #Load Exif Info & Modify
            try:
                exif_dict = piexif.load(image.info["exif"])
                exif_ifd = exif_dict["Exif"]
            except:
                self.debug("Failed loading exif data")
                        
            if image.mode in ('RGBA', 'LA'):
                image = image.convert("RGB")
                
            w, h = image.size
            zeroth_ifd[piexif.ImageIFD.Make] = u"Tadpoles"
            zeroth_ifd[piexif.ImageIFD.XResolution] = (w, 1)
            zeroth_ifd[piexif.ImageIFD.YResolution] = (h, 1)
                        
            eastern = timezone('America/New_York')
            date_taken = datetime.datetime.fromtimestamp(timestamp,eastern)
            exif_ifd[piexif.ExifIFD.DateTimeOriginal] = date_taken.strftime('%Y:%m:%d %H:%M:%S %Z')

            exif_dict["0th"] = zeroth_ifd
            exif_dict["Exif"] = exif_ifd
            
            #Dump to new object and return
            exif_bytes = piexif.dump(exif_dict)
            output_image = io.BytesIO()
            
            image.save(output_image, format="JPEG", exif=exif_bytes, subsampling=-1, quality=95, progressive=True)
            return output_image
        except Exception as exc:
            self.debug("Failed to process exif data")
            self.exception(exc)
            return image
        
    def write_s3(self,file, filename, mime_type, rewind=False):
        blob = self.BUCKET.blob(filename)
        blob.upload_from_file(file, rewind=rewind,content_type=mime_type)

    def save_image_api(self, key, timestamp, mime_type):
        year = datetime.datetime.utcfromtimestamp(timestamp).strftime('%Y')
        month = datetime.datetime.utcfromtimestamp(timestamp).strftime('%b')

        url = self.ROOT_URL + "remote/v1/attachment?key="+key+"&download=true"

        #Download file
        coolCookie = os.getenv("COOKIE")
        email=os.getenv("EMAIL")
        HEADERS = {
            'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:69.0) Gecko/20100101 Firefox/69.0',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'X-TADPOLES-UID': email,
            'X-Requested-With': 'XMLHttpRequest',
            'Connection': 'keep-alive',
            'Referer': 'https://www.tadpoles.com/parents',
            'cookie': coolCookie
        }

        resp = requests.get(url, headers=HEADERS, stream=True)
        if resp.status_code != 200:
            msg = 'Error (%r) downloading %r'
            raise DownloadError(msg % (resp.status_code, url))

        filename_parts = ['/',year, month, resp.headers['Content-Disposition'].split("filename=")[1]]
        filename = join(*filename_parts)
        
        if mime_type == 'image/jpeg' or mime_type == 'image/png':
            self.debug("Writing image" + filename)
            file = self.write_exif(resp, timestamp)
            self.write_s3(file,filename, mime_type, True)
        else:
            self.debug("Writing video" + filename)
            self.write_s3(resp.raw,filename, mime_type)

    def download_images(self):
        '''Login to tadpoles.com and download all user's images.
        '''
        """   try:
            self.load_cookies_db()
        except FileNotFoundError:
           self.navigate_url(self.HOME_URL)
           self.do_login()
           self.dump_cookies_db()
           self.load_cookies_db()

        # Get the cookies ready for requests lib.
        self.requestify_cookies()
        """
        self.get_api()
    
    def main(self):
        with self as client:
            try:
                client.download_images()
            except Exception as exc:
                self.exception(exc)

def download_images():
    Client().main()

if __name__ == "__main__":
    download_images()
