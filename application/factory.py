# -*- coding: utf-8 -*-

import json
import os
import logging
import random
import re
import string

import botocore
from dynaconf import FlaskDynaconf
from flask import Flask, abort, request, redirect, Response, jsonify
from flask_cors import CORS
from flask_cors.core import probably_regex, try_match_any
from flask_csp import CSP
from slugify import slugify
from sentry_sdk.integrations.flask import FlaskIntegration

from application import stripe
from application.exceptions import setup_sentry
from application.eleventy import Flask11tyServerless, LambdaMessageEncoder
from application.authorizer import FlaskJSONAuthorizer
from application.redirects import FlaskJSONRedirects
from application.s3proxy import FlaskS3Proxy
from application.localizer import FlaskLocalizer
from application.utils import forced_relative_redirect, init_extension


def origins_list_to_regex(origins):
    logging.info(f'Original origins list: {origins}')
    if not isinstance(origins, (list, set, tuple,)):
        if isinstance(origins, str) and origins.startswith('[') and origins.endswith(']'):
            origins = json.loads(origins)
        else:
            origins = [origins]

    regex_list = []
    for string in origins:
        if not string.startswith('http://') and not string.startswith('https://'):
            string = f'https://{string}'

        if probably_regex(string):
            regex_list.append(re.compile(rf'{string}'))
        else:
            regex_list.append(string)

    return regex_list


def create_app(name, log_level=logging.WARN):
    app = Flask(name, static_folder=None)
    app.url_map.strict_slashes = False

    FlaskDynaconf(app)

    app.logger.setLevel(log_level)
    app.logger.propagate = True

    if app.config["ENV"].lower() not in ("development", "testing"):
        setup_sentry(
            app.config.get("SENTRY_DSN"),
            debug=app.debug,
            integrations=[
                FlaskIntegration(),
            ],
            environment=app.config["ENV"],
            request_bodies="always",
        )

    stripe.init_app(app)

    app.allowed_origins = origins_list_to_regex(app.config.get('ALLOWED_ORIGINS', ['.*']))
    CORS(app, origins=app.allowed_origins, supports_credentials=True)
    CSP(app)

    app.s3_proxy = FlaskS3Proxy(app)
    app.localizer = FlaskLocalizer(app)

    app.authorizer = init_extension(app, FlaskJSONAuthorizer, 'S3_AUTHORIZER_FILE')
    app.eleventy = init_extension(app, Flask11tyServerless, 'S3_ELEVENTY_FILE')
    app.redirects = init_extension(app, FlaskJSONRedirects, 'S3_REDIRECTS_FILE')

    # Due to the redirects possibly using these routes, we are adding these after having
    # instantiated all the redirects. If not for that, we could have used a config value
    app.s3_proxy.add_handled_routes(['/', '/<path:url>'], methods=['GET', 'POST'])

    def is_allowed_origin():
        if app.allowed_origins:
            origin = request.headers.get('Origin')

            if not origin:
                app.logger.debug('Origin header not provided')
                return False

            if (
                    not try_match_any(origin, app.allowed_origins) and
                    not try_match_any(f'{origin}/', app.allowed_origins)
            ):
                app.logger.debug('Origin header not in allowed list: {}'.format(origin))
                return False

        return True

    @app.before_request
    def clear_trailing():
        rp = request.path
        if rp != '/' and rp.endswith('/'):
            return forced_relative_redirect(rp[:-1], code=302)

    @app.before_request
    def chk_shortcircuit():
        if request.method == 'OPTION' and app.config['SHORTCIRCUIT_OPTIONS']:
            app.logger.debug('Shortcircuiting OPTIONS request')
            if is_allowed_origin():
                return '', 200
            return abort(403)

        # If we get here, we're neither shortcircuiting OPTIONS requests, let the view
        # deal with it directly.
        return None

    if app.config['ADD_CACHE_HEADERS']:

        @app.after_request
        def add_cache_headers(response):
            if response.status_code != 200:
                return response

            content_type = response.headers.get('Content-Type')
            if 'text' in content_type or 'application' in content_type:
                return response

            if not response.headers.get('Cache-Control'):
                response.headers['Cache-Control'] = 'public,max-age=2592000,s-maxage=2592000,immutable'
            if not response.headers.get('Vary'):
                response.headers['Vary'] = 'Accept-Encoding,Origin,Access-Control-Request-Headers,Access-Control-Request-Method'

            return response

    return app
