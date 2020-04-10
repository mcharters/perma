from contextlib import contextmanager
from collections import OrderedDict
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from functools import wraps, reduce
import hashlib
from hanzo import warctools
import itertools
import json
import logging
from nacl import encoding
from nacl.public import Box, PrivateKey, PublicKey
from netaddr import IPAddress, IPNetwork
import operator
import os
import requests
import socket
import string
import surt
import tempdir
import tempfile
from ua_parser import user_agent_parser
import unicodedata
from urllib.parse import urlparse
from warcio.warcwriter import BufferWARCWriter
from wsgiref.util import FileWrapper

from django.core.paginator import Paginator
from django.db.models import Q
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponseForbidden, Http404, StreamingHttpResponse
from django.utils.decorators import available_attrs
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .exceptions import InvalidTransmissionException, WebrecorderException

logger = logging.getLogger(__name__)
warn = logger.warn


### celery helpers ###


def run_task(task, *args, **kwargs):
    """
        Run a celery task either async or directly, depending on settings.RUN_TASKS_ASYNC.
    """
    options = kwargs.pop('options', {})
    if settings.RUN_TASKS_ASYNC:
        return task.apply_async(args, kwargs, **options)
    else:
        return task.apply(args, kwargs, **options)

### login helper ###
def user_passes_test_or_403(test_func):
    """
    Decorator for views that checks that the user passes the given test,
    raising PermissionDenied if not. Based on Django's user_passes_test.
    The test should be a callable that takes the user object and
    returns True if the user passes.
    """
    def decorator(view_func):
        @login_required()
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            if not test_func(request.user):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator

### password helper ###

class AlphaNumericValidator:
    """
    Adapted from https://djangosnippets.org/snippets/2551/
    """

    @sensitive_variables()
    def validate(self, password, user=None):
        contains_number = contains_letter = False
        for char in password:
            if not contains_number:
                if char in string.digits:
                    contains_number = True
            if not contains_letter:
                if char in string.ascii_letters:
                    contains_letter = True
            if contains_number and contains_letter:
                break

        if not contains_number or not contains_letter:
            raise ValidationError("This password does not include at least \
                                   one letter and at least one number.")

    def get_help_text(self):
        return "Your password must include at least \
                one letter and at least one number."


### list view helpers ###

def apply_search_query(request, queryset, fields):
    """
        For the given `queryset`,
        apply consecutive .filter()s such that each word
        in request.GET['q'] appears in one of the `fields`.
    """
    search_string = request.GET.get('q', '')
    if not search_string:
        return queryset, ''

    # get words in search_string
    required_words = search_string.strip().split()
    if not required_words:
        return queryset

    for required_word in required_words:
        # apply the equivalent of queryset = queryset.filter(Q(field1__icontains=required_word) | Q(field2__icontains=required_word) | ...)
        query_parts = [Q(**{field+"__icontains":required_word}) for field in fields]
        query_parts_joined = reduce(operator.or_, query_parts, Q())
        queryset = queryset.filter(query_parts_joined)

    return queryset, search_string

def apply_sort_order(request, queryset, valid_sorts, default_sort=None):
    """
        For the given `queryset`,
        apply sort order based on request.GET['sort'].
    """
    if not default_sort:
        default_sort = valid_sorts[0]
    sort = request.GET.get('sort', default_sort)
    if sort not in valid_sorts:
        sort = default_sort
    return queryset.order_by(sort), sort

def apply_pagination(request, queryset):
    """
        For the given `queryset`,
        apply pagination based on request.GET['page'].
    """
    try:
        page = max(int(request.GET.get('page', 1)), 1)
    except ValueError:
        page = 1
    paginator = Paginator(queryset, settings.MAX_USER_LIST_SIZE)
    return paginator.page(page)

### form view helpers ###

def get_form_data(request):
    return request.POST if request.method == 'POST' else None

### debug toolbar ###

def show_debug_toolbar(request):
    """ Used by django-debug-toolbar in settings_dev.py to decide whether to show debug toolbar. """
    return settings.DEBUG

### image manipulation ###

@contextmanager
def imagemagick_temp_dir():
    """
        Inside this context manager, the environment variable MAGICK_TEMPORARY_PATH will be set to a
        temp path that gets deleted when the context closes. This stops Wand's calls to ImageMagick
        leaving temp files around.
    """
    temp_dir = tempdir.TempDir()
    old_environ = dict(os.environ)
    os.environ['MAGICK_TEMPORARY_PATH'] = temp_dir.name
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
        temp_dir.dissolve()

### caching ###

# via: http://stackoverflow.com/a/9377910/313561
def if_anonymous(decorator):
    """ Returns decorated view if user is not admin. Un-decorated otherwise """

    def _decorator(view):

        decorated_view = decorator(view)  # This holds the view with cache decorator

        def _view(request, *args, **kwargs):

            if request.user.is_authenticated:
                return view(request, *args, **kwargs)  # view without @cache
            else:
                return decorated_view(request, *args, **kwargs) # view with @cache

        return _view

    return _decorator

### file manipulation ###

def copy_file_data(from_file_handle, to_file_handle, chunk_size=1024*100):
    """
        Copy data from first file handle to second file handle in memory-efficient way.
    """
    while True:
        data = from_file_handle.read(chunk_size)
        if not data:
            break
        to_file_handle.write(data)

### rate limiting ###

def ratelimit_ip_key(group, request):
    return get_client_ip(request)

### security ###

def ip_in_allowed_ip_range(ip):
    """ Return False if ip is blocked. """
    if not ip:
        return False
    ip = IPAddress(ip)
    for banned_ip_range in settings.BANNED_IP_RANGES:
        if IPAddress(ip) in IPNetwork(banned_ip_range):
            return False
    return True

def url_in_allowed_ip_range(url):
    """ Return False if url resolves to a blocked IP. """
    hostname = urlparse(url).netloc.split(':')[0]
    try:
        ip = socket.gethostbyname(hostname)
    except socket.gaierror:
        return False
    return ip_in_allowed_ip_range(ip)

def get_client_ip(request):
    return request.META[settings.CLIENT_IP_HEADER]

### dates and times ###

def tz_datetime(*args, **kwargs):
    return timezone.make_aware(datetime(*args, **kwargs))


def first_day_of_next_month(now):
    # use first of month instead of today to avoid issues w/ variable length months
    first_of_month = now.replace(day=1)
    return first_of_month + relativedelta(months=1)


def today_next_year(now):
    # relativedelta handles leap years: 2/29 -> 2/28
    return now + relativedelta(years=1)


### addresses ###

def get_lat_long(address):
    r = None
    try:
        r = requests.get('https://maps.googleapis.com/maps/api/geocode/json', {'address': address, 'key':settings.GEOCODING_KEY})
    except Exception as e:
        warn("Error connecting to geocoding API: {}".format(e))
    if r and r.status_code == 200:
        rj = r.json()
        status = rj['status']
        if status == 'OK':
            results = rj['results']
            if len(results) == 1:
                (lat, lng) = (results[0]['geometry']['location']['lat'], results[0]['geometry']['location']['lng'])
                return (lat, lng)
            else:
                warn("Multiple locations returned for address.")
        elif status == 'ZERO_RESULTS':
            warn("No location returned for address.")
        elif status == 'REQUEST_DENIED':
            warn("Geocoding API request denied.")
        elif status == 'OVER_QUERY_LIMIT':
            warn("Geocoding API request over query limit.")
        else:
            warn("Unknown response from geocoding API: {}".format(status))
    else:
        warn("Error connecting to geocoding API: {}".format(r.status_code))


def parse_user_agent(user_agent_str):
    # if user_agent_str is unparseable, will return:
    # {'brand': None, 'model': None, 'family': 'Other'}
    return user_agent_parser.ParseUserAgent(user_agent_str)


### pdf handling on mobile ###

def redirect_to_download(capture_mime_type, user_agent_str):
    # redirecting to a page with a download button (and not attempting to display)
    # if mobile apple device, and the request is a pdf
    parsed_agent = parse_user_agent(user_agent_str)

    return parsed_agent["family"] and capture_mime_type and \
           "Mobile" in parsed_agent["family"] and "pdf" in capture_mime_type


### playback

def protocol():
    return "https://" if settings.SECURE_SSL_REDIRECT else "http://"

### memento

def url_with_qs_and_hash(url, qs_and_hash=None):
    if qs_and_hash:
        url = f"{url}?{qs_and_hash}"
    return url

def url_split(url):
    """ Separate into base and query + hash"""
    return url.split('?', 1)

def timemap_url(request, url, response_format):
    base, *qs_and_hash = url_split(url)
    return url_with_qs_and_hash(
        request.build_absolute_uri(reverse('timemap', args=[response_format, base])),
        qs_and_hash[0] if qs_and_hash else ''
    )

def timegate_url(request, url):
    base, *qs_and_hash = url_split(url)
    return url_with_qs_and_hash(
        request.build_absolute_uri(reverse('timegate', args=[base])),
        qs_and_hash[0] if qs_and_hash else ''
    )

def memento_url(request, link):
    return request.build_absolute_uri(reverse('single_permalink', args=[link.guid]))

def memento_data_for_url(request, url, qs=None, hash=None):
    from perma.models import Link  #noqa
    try:
        canonicalized = surt.surt(url)
    except ValueError:
        return {}
    mementos = [
        {
            'uri': memento_url(request, link),
            'datetime': link.creation_timestamp,
        } for link in Link.objects.visible_to_memento().filter(submitted_url_surt=canonicalized).order_by('creation_timestamp')
    ]
    if not mementos:
        return {}
    return {
        'self': request.build_absolute_uri(),
        'original_uri': url,
        'timegate_uri': timegate_url(request, url),
        'timemap_uri': {
            'json_format': timemap_url(request, url, 'json'),
            'link_format': timemap_url(request, url, 'link'),
            'html_format': timemap_url(request, url, 'html'),
        },
        'mementos': {
            'first': mementos[0],
            'last': mementos[-1],
            'list': mementos,
        }
    }


def remove_control_characters(s):
    return "".join(ch for ch in s if unicodedata.category(ch)[0]!="C")

### perma payments

# communication

@sensitive_variables()
def prep_for_perma_payments(dictionary):
    return encrypt_for_perma_payments(stringify_data(dictionary))


@sensitive_variables()
def process_perma_payments_transmission(transmitted_data, fields):
    # Transmitted data should contain a single field, 'encrypted data', which
    # must be a JSON dict, encrypted by Perma-Payments and base64-encoded.
    encrypted_data = transmitted_data.get('encrypted_data', '')
    if not encrypted_data:
        raise InvalidTransmissionException('No encrypted_data in POST.')
    try:
        post_data = unstringify_data(decrypt_from_perma_payments(encrypted_data))
    except Exception as e:
        logger.warning('Problem with transmitted data. {}'.format(format_exception(e)))
        raise InvalidTransmissionException(format_exception(e))

    # The encrypted data must include a valid timestamp.
    try:
        timestamp = post_data['timestamp']
    except KeyError:
        logger.warning('Missing timestamp in data.')
        raise InvalidTransmissionException('Missing timestamp in data.')
    if not is_valid_timestamp(timestamp, settings.PERMA_PAYMENTS_TIMESTAMP_MAX_AGE_SECONDS):
        logger.warning('Expired timestamp in data.')
        raise InvalidTransmissionException('Expired timestamp in data.')

    return retrieve_fields(post_data, fields)


# helpers

def pp_date_from_post(posted_value):
    if posted_value:
        return datetime.strptime(posted_value, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
    return None


def format_exception(e):
    return "{}: {}".format(type(e).__name__, e)


@sensitive_variables()
def retrieve_fields(transmitted_data, fields):
    try:
        data = {}
        for field in fields:
            data[field] = transmitted_data[field]
    except KeyError as e:
        msg = 'Incomplete data received: missing {}'.format(e)
        logger.warning(msg)
        raise InvalidTransmissionException(msg)
    return data


def is_valid_timestamp(stamp, max_age):
    return stamp <= (datetime.utcnow() + timedelta(seconds=max_age)).timestamp()


@sensitive_variables()
def stringify_data(data):
    """
    Takes any json-serializable data. Converts to a bytestring, suitable for passing to an encryption function.
    """
    return bytes(json.dumps(data, cls=DjangoJSONEncoder), 'utf-8')


@sensitive_variables()
def unstringify_data(data):
    """
    Reverses stringify_data. Takes a bytestring, returns deserialized json.
    """
    return json.loads(str(data, 'utf-8'))


@sensitive_variables()
def encrypt_for_perma_payments(message, encoder=encoding.Base64Encoder):
    """
    Basic public key encryption ala pynacl.
    """
    box = Box(
        PrivateKey(
            settings.PERMA_PAYMENTS_ENCRYPTION_KEYS['perma_secret_key'], encoder=encoder
        ),
        PublicKey(
            settings.PERMA_PAYMENTS_ENCRYPTION_KEYS['perma_payments_public_key'], encoder=encoder
        )
    )
    return box.encrypt(message, encoder=encoder)


@sensitive_variables()
def decrypt_from_perma_payments(ciphertext, encoder=encoding.Base64Encoder):
    """
    Decrypt bytes encrypted by perma-payments.
    """
    box = Box(
        PrivateKey(
            settings.PERMA_PAYMENTS_ENCRYPTION_KEYS['perma_secret_key'], encoder=encoder
        ),
        PublicKey(
            settings.PERMA_PAYMENTS_ENCRYPTION_KEYS['perma_payments_public_key'], encoder=encoder
        )
    )
    return box.decrypt(ciphertext, encoder=encoder)

#
# warc writing
#

@contextmanager
def preserve_perma_warc(guid, timestamp, destination, warc_size):
    """
    Context manager for opening a perma warc, ready to receive warc records.
    Safely closes and saves the file to storage when context is exited.
    """
    # mode set to 'ab+' as a workaround for https://bugs.python.org/issue25341
    out = tempfile.TemporaryFile('ab+')
    write_perma_warc_header(out, guid, timestamp)
    try:
        yield out
    finally:
        out.flush()
        warc_size.append(out.tell())
        out.seek(0)
        default_storage.store_file(out, destination, overwrite=True)
        out.close()

def write_perma_warc_header(out_file, guid, timestamp):
    # build warcinfo header
    headers = [
        (warctools.WarcRecord.ID, warctools.WarcRecord.random_warc_uuid()),
        (warctools.WarcRecord.TYPE, warctools.WarcRecord.WARCINFO),
        (warctools.WarcRecord.DATE, warctools.warc.warc_datetime_str(timestamp))
    ]
    warcinfo_fields = [
        b'operator: Perma.cc',
        b'format: WARC File Format 1.0',
        bytes('Perma-GUID: {}'.format(guid), 'utf-8')
    ]
    data = b'\r\n'.join(warcinfo_fields) + b'\r\n'
    warcinfo_record = warctools.WarcRecord(headers=headers, content=(b'application/warc-fields', data))
    warcinfo_record.write_to(out_file, gzip=True)


def make_detailed_warcinfo(filename, guid, coll_title, coll_desc, rec_title, pages):
    # #
    # Thank you! Rhizome/Webrecorder.io/Ilya Kreymer
    # #

    coll_metadata = {'type': 'collection',
                     'title': coll_title,
                     'desc': coll_desc}

    rec_metadata = {'type': 'recording',
                    'title': rec_title,
                    'pages': pages}

    # Coll info
    writer = BufferWARCWriter(gzip=True)
    params = OrderedDict([('operator', 'Perma.cc download'),
                          ('Perma-GUID', guid),
                          ('format', 'WARC File Format 1.0'),
                          ('json-metadata', json.dumps(coll_metadata))])

    record = writer.create_warcinfo_record(filename, params)
    writer.write_record(record)

    # Rec Info
    params['json-metadata'] = json.dumps(rec_metadata)

    record = writer.create_warcinfo_record(filename, params)
    writer.write_record(record)

    return writer.get_contents()


def write_warc_records_recorded_from_web(source_file_handle, out_file):
    """
    Copies a series of pre-recorded WARC Request/Response records to out_file
    """
    copy_file_data(source_file_handle, out_file)


def write_resource_record_from_asset(data, url, content_type, out_file, extra_headers=None):
    """
    Constructs a single WARC resource record from an asset (screenshot, uploaded file, etc.)
    and writes to out_file.
    """
    warc_date = warctools.warc.warc_datetime_str(timezone.now()).replace(b'+00:00Z', b'Z')
    headers = [
        (warctools.WarcRecord.TYPE, warctools.WarcRecord.RESOURCE),
        (warctools.WarcRecord.ID, warctools.WarcRecord.random_warc_uuid()),
        (warctools.WarcRecord.DATE, warc_date),
        (warctools.WarcRecord.URL, bytes(url, 'utf-8')),
        (warctools.WarcRecord.BLOCK_DIGEST, bytes('sha1:{}'.format(hashlib.sha1(data).hexdigest()), 'utf-8'))
    ]
    if extra_headers:
        headers.extend(extra_headers)
    record = warctools.WarcRecord(headers=headers, content=(bytes(content_type, 'utf-8'), data))
    record.write_to(out_file, gzip=True)

def get_warc_stream(link):
    filename = "%s.warc.gz" % link.guid

    timestamp = link.creation_timestamp.strftime('%Y%m%d%H%M%S')

    warcinfo = make_detailed_warcinfo(
        filename = filename,
        guid = link.guid,
        coll_title = 'Perma Archive, %s' % link.submitted_title,
        coll_desc = link.submitted_description,
        rec_title = 'Perma Archive of %s' % link.submitted_title,
        pages= [{
            'title': link.submitted_title,
            'url': link.submitted_url,
            'timestamp': timestamp
        }]
    )

    warc_stream = FileWrapper(default_storage.open(link.warc_storage_file()))
    warc_stream = itertools.chain([warcinfo], warc_stream)
    response = StreamingHttpResponse(warc_stream, content_type="application/gzip")
    response['Content-Disposition'] = 'attachment; filename="%s"' % filename

    return response

def stream_warc(link):
    # `link.user_deleted` is checked here for dev convenience:
    # it's easy to forget that deleted links/warcs aren't truly deleted,
    # and easy to accidentally permit the downloading of "deleted" warcs.
    # Users of stream_warc shouldn't have to worry about / remember this.
    if link.user_deleted or not link.can_play_back():
        raise Http404
    return get_warc_stream(link)

def stream_warc_if_permissible(link, user):
    if user.can_view(link):
        return stream_warc(link)
    return HttpResponseForbidden('Private archive.')


#
# Webrecorder Helpers
#

def clear_wr_session(request, error_if_wr_user_not_found=False):
    """
    Clear Webrecorder session info in Perma and in WR
    """
    wr_temp_username = request.session.pop('wr_temp_username', '')
    wr_private_session_cookie = request.session.pop('wr_private_session_cookie', '')
    request.session.pop('wr_public_session_cookie', '')
    request.session.save()

    if not wr_temp_username or not wr_private_session_cookie:
        return

    try:
        response, _  = query_wr_api(
            method='delete',
            path='/user/{user}'.format(user=wr_temp_username),
            cookie=wr_private_session_cookie,
            valid_if=lambda code, data: (code == 200) or (code == 404 and data.get('error') in ['not_found', 'no_such_user'])
        )
    except WebrecorderException:
        # Record the exception, but don't halt execution: this should be non-fatal
        logger.exception('Unexpected response from DELETE /user/{user}'.format(user=wr_temp_username))
        return

    if response.status_code == 404:
        if error_if_wr_user_not_found:
            log_level = logging.ERROR
        else:
            log_level = logging.INFO
        logger.log(log_level, 'Attempt to delete {} from WR failed: already expired?'.format(wr_temp_username))


def query_wr_api(method, path, cookie, valid_if, json=None, data=None):
    # Make the request
    try:
        response = requests.request(
            method,
            settings.WR_API + path,
            json=json,
            data=data,
            cookies={'__wr_sesh': cookie} if cookie else None,
            timeout=10,
            allow_redirects=False
        )
    except requests.exceptions.RequestException as e:
        raise WebrecorderException() from e

    # Validate the response
    try:
        data = safe_get_response_json(response)
        assert valid_if(response.status_code, data)
    except AssertionError:
        raise WebrecorderException("{code}: {message}".format(
            code=response.status_code,
            message=str(data)
        ))

    return response, data


def get_wr_session_cookie(request, session_key):
    cookie = request.session.get(session_key)
    timestamp = request.session.get(session_key + '_timestamp')
    if cookie and timestamp >= (datetime.utcnow() - timedelta(seconds=settings.WR_COOKIE_PERMITTED_AGE)).timestamp():
        return cookie
    return None


def safe_get_response_json(response):
    try:
        data = response.json()
    except ValueError:
        data = {}
    return data


def set_options_headers(request, response, always_set_allowed_origin=False):
    '''
    Mutates response in-place.
    '''

    origin = request.META.get('HTTP_ORIGIN')
    origin_host = settings.PLAYBACK_HOST
    target_host = settings.HOST

    # always set access-control-allow-origin if requested
    if always_set_allowed_origin and origin_host:
        expected_origin = request.scheme + '://' + origin_host
        response['Access-Control-Allow-Origin'] = expected_origin

    # no origin, not using cors
    if not origin:
        return False

    if origin_host:
        expected_origin = request.scheme + '://' + origin_host

        # ensure origin is the content host origin
        if origin != expected_origin:
            return False

    host = request.META.get('HTTP_HOST')
    # ensure host is the app host
    if target_host and host != target_host:
        return False

    response['Access-Control-Allow-Origin'] = origin

    methods = request.META.get('HTTP_ACCESS_CONTROL_REQUEST_METHOD')
    if methods:
        response['Access-Control-Allow-Methods'] = methods

    headers = request.META.get('HTTP_ACCESS_CONTROL_REQUEST_HEADERS')
    if headers:
        response['Access-Control-Allow-Headers'] = headers

    response['Access-Control-Allow-Credentials'] = 'true'
