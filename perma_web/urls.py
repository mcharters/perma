from django.conf.urls import handler500, url, include
from django.conf import settings
from django.contrib import admin
from django.views.defaults import page_not_found

# Setting our custom route handler so that images are displayed properly
# Used implicitly by Django
handler500 = 'perma.views.error_management.server_error'  # noqa

urlpatterns = [
    url(r'^admin/', admin.site.urls),  # Django admin
    url(r'^api/', include('api.urls')), # Our API mirrored for session access
    url(r'^lockss/', include('lockss.urls', namespace='lockss')), # Our app that communicates with the mirror network
    url(r'^', include('perma.urls')), # The Perma app
]

if settings.API_ONLY:
    urlpatterns = [
        url(r'^admin/', page_not_found),  # Django admin
        url(r'^api/', include('api.urls')), # Our API mirrored for session access
        url(r'^lockss/', page_not_found), # Our app that communicates with the mirror network
        url(r'^', page_not_found), # The Perma app
        url(r'^$', page_not_found)
    ]
