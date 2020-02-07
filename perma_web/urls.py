from django.conf.urls import handler500, handler404, url, include
from django.conf import settings
from django.contrib import admin
from django.views.defaults import page_not_found

# Setting our custom route handler so that images are displayed properly
# Used implicitly by Django
handler500 = 'perma.views.error_management.server_error'  # noqa

if settings.API_ONLY:
    # custom 404 view that doesn't try to redirect to any other page
    handler404 = 'perma.views.error_management.api_only_404'
    urlpatterns = [
        url(r'^api/', include('api.urls')), # Our API mirrored for session access
    ]
else:
    urlpatterns = [
    url(r'^admin/', admin.site.urls),  # Django admin
    url(r'^api/', include('api.urls')), # Our API mirrored for session access
    url(r'^lockss/', include('lockss.urls', namespace='lockss')), # Our app that communicates with the mirror network
    url(r'^', include('perma.urls')), # The Perma app
]