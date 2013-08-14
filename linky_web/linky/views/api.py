import logging, json, subprocess, urllib2, re, os
from datetime import datetime
from urlparse import urlparse

import lxml.html
#import PythonMagick
from PIL import Image
from pyPdf import PdfFileReader

from linky.models import Link, Asset
from linky.utils import base
from linky.tasks import get_screen_cap, get_source

from django.shortcuts import render_to_response, HttpResponse
from django.http import HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django import forms
from django.conf import settings

logger = logging.getLogger(__name__)

try:
    from linky.local_settings import *
except ImportError, e:
    logger.error('Unable to load local_settings.py: %s', e)

# TODO: If we're going to csrf exempt this, we should keep an eye on things
@csrf_exempt
def linky_post(request):
    """ When we receive a Linky POST """
    target_url = request.POST.get('url')

    if not target_url:
        return HttpResponse(status=400)
        
    if target_url[0:4] != 'http':
        target_url = 'http://' + target_url

    url_details = urlparse(target_url)

    target_title = url_details.netloc
        
    try:
        parsed_html = lxml.html.parse(urllib2.urlopen(target_url))
    except IOError:
        pass
    
    if parsed_html:
        if parsed_html.find(".//title") is not None and parsed_html.find(".//title").text:
            target_title = parsed_html.find(".//title").text
    
    link = Link(submitted_url=target_url, submitted_title=target_title)
    
    if request.user.is_authenticated():
        link.created_by = request.user

    link.save()
    
    linky_hash = base.convert(link.id, base.BASE10, base.BASE58)
    
    # Assets get stored in /storage/path/year/month/day/hour/unique-id/*
    now = dt = datetime.now()
    time_tuple = now.timetuple()
    
    path_elements = [str(time_tuple.tm_year), str(time_tuple.tm_mon), str(time_tuple.tm_mday), str(time_tuple.tm_hour), str(link.id)]
    
    # We store our assets on disk. Create a home for them
    #if not os.path.exists(os.path.sep.join(path_elements)):
    #    os.makedirs(os.path.sep.join(path_elements))

    # Create a stub for our assets
    asset, created = Asset.objects.get_or_create(link=link)
    asset.base_storage_path = os.path.sep.join(path_elements)
    asset.save()
    

    # Run our synchronus screen cap task (use the headless browser to create a static image) 
    get_screen_cap(link.id, target_url, os.path.sep.join(path_elements))
    
    try:
        get_source.delay(link.id, target_url, os.path.sep.join(path_elements), request.META['HTTP_USER_AGENT'])
    except Exception, e:
        # TODO: Log the failed url
        asset.warc_capture = 'failed'
        asset.save()
        
    asset= Asset.objects.get(link__id=link.id)

    # TODO: roll the favicon function into a pimp python package
    #favicon_success = __get_favicon(target_url, parsed_html, link.id, linky_home_disk_path, url_details)
    
    response_object = {'linky_id': linky_hash, 'linky_cap': settings.STATIC_URL + asset.base_storage_path + '/' + asset.image_capture, 'linky_title': link.submitted_title}
    
    if False: #favicon_success:
        response_object['favicon_url'] = 'http://' + request.get_host() + '/static/generated/' + str(link.id) + '/' + favicon_success
        manifest['favicon'] = favicon_success

    return HttpResponse(json.dumps(response_object), content_type="application/json", status=201)

def __get_favicon(target_url, parsed_html, link_hash_id, disk_path, url_details):
    """ Given a URL and the markup, see if we can find a favicon.
        TODO: this is a rough draft. cleanup and move to an appropriate place. """
    
    # We already have the parsed HTML, let's see if there is a favicon in the META elements
    favicons = parsed_html.xpath('//link[@rel="icon"]/@href')
    
    favicon = False
    
    if len(favicons) > 0:
        favicon = favicons[0]
        
    if not favicon:
        favicons = parsed_html.xpath('//link[@rel="shortcut"]/@href')
        if len(favicons) > 0:
            favicon = favicons[0]

    if not favicon:
        favicons = parsed_html.xpath('//link[@rel="shortcut icon"]/@href')
        if len(favicons) > 0:
            favicon = favicons[0]

    if favicon:
            
        if re.match(r'^//', favicon):
            favicon = url_details.scheme + ':' + favicon
        elif not re.match(r'^http', favicon):
            favicon = url_details.scheme + '://' + url_details.netloc + favicon
                
        f = urllib2.urlopen(favicon)
        data = f.read()
        
        
        filepath_pieces = os.path.splitext(favicon)
        file_ext = filepath_pieces[1]
        
        with open(disk_path + 'fav' + file_ext, "wb") as asset:
            asset.write(data)

        return 'fav' + file_ext


    # If we haven't returned True above, we didn't find a favicon in the markup.
    # let's try the favicon convention: http://example.com/favicon.ico
    target_favicon_url = url_details.scheme + '://' + url_details.netloc + '/favicon.ico'
    
    try:
        f = urllib2.urlopen(target_favicon_url)
        data = f.read()
        with open(disk_path + 'fav.ico' , "wb") as asset:
            asset.write(data)

        return 'fav' + '.ico'
    except urllib2.HTTPError:
        pass
        
        
    return False

class UploadFileForm(forms.Form):
    title = forms.CharField(max_length=50, required=True)
    url = forms.URLField(required=True)
    file  = forms.FileField(required=True)
    favicon  = forms.FileField(required=False)

def validate_favicon(upload):
    # TODO: Figure out how to actually validate a favicon.
    if upload.name == 'favicon.ico':
        return True
    return False

def validate_upload_file(upload):
    # Make sure files are not corrupted.
    extention = upload.name[-4:]
    if extention in ['.jpg', 'jpeg']:
        try:
            i = Image.open(upload)
            if i.format == 'JPEG':
                return True
        except IOError:
            return False
    elif extention == '.png':
        try:
            i = Image.open(upload)
            if i.format =='PNG':
                return True
        except IOError:
            return False
    elif extention == '.pdf':
        doc = PdfFileReader(upload)
        if all([doc.documentInfo, doc.numPages]):
            return True
    return False

@csrf_exempt
def upload_file(request):
    if request.method == 'POST':
        form = UploadFileForm(request.POST, request.FILES)
        if form.is_valid(): 
            if validate_upload_file(request.FILES['file']):
                link = Link(submitted_url=form.cleaned_data['url'], submitted_title=form.cleaned_data['title'])
                link.save()
                linky_home_disk_path = settings.PROJECT_ROOT +'/'+ '/static/generated/' + str(link.id) + '/'

                if not os.path.exists(linky_home_disk_path):
                    os.makedirs(linky_home_disk_path)
        
                linky_hash = base.convert(link.id, base.BASE10, base.BASE58)
                file_name = 'cap.' + request.FILES['file'].name.split('.')[-1]
                request.FILES['file'].file.seek(0)
                f = open(linky_home_disk_path + file_name, 'w')
                f.write(request.FILES['file'].file.read())
                os.fsync(f)
                f.close()
                if request.FILES['file'].name.split('.')[-1] == 'pdf':
                    pass
                    #print linky_home_disk_path + file_name
                    #png = PythonMagick.Image(linky_home_disk_path + file_name)
                    #png.write("file_out.png")
                    #params = ['convert', linky_home_disk_path + file_name, 'out.png']
                    #subprocess.check_call(params)

                return HttpResponse(json.dumps({'status':'success', 'linky_id':link.id, 'linky_hash':linky_hash}), 'application/json')
            else:
                return HttpResponseBadRequest(json.dumps({'status':'failed', 'reason':'Invalid file.'}), 'application/json')
        else:
            return HttpResponseBadRequest(json.dumps({'status':'failed', 'reason':'Missing file.'}), 'application/json')
    return HttpResponseBadRequest(json.dumps({'status':'failed', 'reason':form.errors}), 'application/json')