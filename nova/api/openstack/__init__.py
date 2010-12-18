# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
WSGI middleware for OpenStack API controllers.
"""

import json
import time

import logging
import routes
import traceback
import webob.dec
import webob.exc
import webob

from nova import context
from nova import flags
from nova import utils
from nova import wsgi
from nova.api.openstack import faults
from nova.api.openstack import backup_schedules
from nova.api.openstack import flavors
from nova.api.openstack import images
from nova.api.openstack import ratelimiting
from nova.api.openstack import servers
from nova.api.openstack import sharedipgroups
from nova.auth import manager


FLAGS = flags.FLAGS
flags.DEFINE_string('nova_api_auth',
    'nova.api.openstack.auth.BasicApiAuthManager',
    'The auth mechanism to use for the OpenStack API implemenation')

flags.DEFINE_string('os_api_ratelimiting',
    'nova.api.openstack.ratelimiting.BasicRateLimiting',
    'Default ratelimiting implementation for the Openstack API')

flags.DEFINE_bool('allow_admin_api',
    False,
    'When True, this API service will accept admin operations.')


class API(wsgi.Middleware):
    """WSGI entry point for all OpenStack API requests."""

    def __init__(self):
        app = AuthMiddleware(RateLimitingMiddleware(APIRouter()))
        super(API, self).__init__(app)

    @webob.dec.wsgify
    def __call__(self, req):
        try:
            return req.get_response(self.application)
        except Exception as ex:
            logging.warn("Caught error: %s" % str(ex))
            logging.debug(traceback.format_exc())
            exc = webob.exc.HTTPInternalServerError(explanation=str(ex))
            return faults.Fault(exc)


class AuthMiddleware(wsgi.Middleware):
    """Authorize the openstack API request or return an HTTP Forbidden."""

    def __init__(self, application):
        self.auth_driver = utils.import_class(FLAGS.nova_api_auth)()
        super(AuthMiddleware, self).__init__(application)

    @webob.dec.wsgify
    def __call__(self, req):
        if not self.auth_driver.has_authentication(req):
            return self.auth_driver.authenticate(req)

        user = self.auth_driver.get_user_by_authentication(req)

        if not user:
            return faults.Fault(webob.exc.HTTPUnauthorized())

        req.environ['nova.context'] = context.RequestContext(user, user)
        return self.application


class RateLimitingMiddleware(wsgi.Middleware):
    """Rate limit incoming requests according to the OpenStack rate limits."""

    def __init__(self, application, service_host=None):
        """Create a rate limiting middleware that wraps the given application.

        By default, rate counters are stored in memory.  If service_host is
        specified, the middleware instead relies on the ratelimiting.WSGIApp
        at the given host+port to keep rate counters.
        """
        super(RateLimitingMiddleware, self).__init__(application)
        self._limiting_driver = \
            utils.import_class(FLAGS.os_api_ratelimiting)(service_host)

    @webob.dec.wsgify
    def __call__(self, req):
       return self._limiting_driver.limited_request(req, self.application) 


class APIRouter(wsgi.Router):
    """
    Routes requests on the OpenStack API to the appropriate controller
    and method.
    """

    def __init__(self):
        mapper = routes.Mapper()
        mapper.resource("server", "servers", controller=servers.Controller(),
                        collection={'detail': 'GET'},
                        member={'action': 'POST'})

        mapper.resource("backup_schedule", "backup_schedules",
                        controller=backup_schedules.Controller(),
                        parent_resource=dict(member_name='server',
                        collection_name='servers'))

        mapper.resource("image", "images", controller=images.Controller(),
                        collection={'detail': 'GET'})
        mapper.resource("flavor", "flavors", controller=flavors.Controller(),
                        collection={'detail': 'GET'})
        mapper.resource("sharedipgroup", "sharedipgroups",
                        controller=sharedipgroups.Controller())

        if FLAGS.allow_admin_api:
            logging.debug("Including admin operations in API.")
            # TODO: Place routes for admin operations here.

        super(APIRouter, self).__init__(mapper)
