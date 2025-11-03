"""
URL configuration for testdjango project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path
from testdjango import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("test/async-user", views.test_async_user),
    path("test/sync-user", views.test_sync_user),
    path("test/async-exception", views.test_async_exception),
    path("test/sync-exception", views.test_sync_exception),
]
