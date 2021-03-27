#!/usr/bin/env python3
#
# Copyright © 2021 Daniel Lenski
#
# This file is part of openconnect.
#
# This is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License
# as published by the Free Software Foundation; either version 2.1 of
# the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>

########################################
# This program emulates the authentication-phase behavior of a Fortinet
# server enough to test OpenConnect's authentication behavior against it.
# Specifically, it emulates the following requests:
#
#    GET /[$REALM]
#    GET /remote/login[?realm=$REALM]
#    POST /remote/logincheck (with username and credential fields)
#      No 2FA)   Completes the login
#      With 2FA) Returns a 2FA challenge
#    POST /remote/logincheck (with username, code, and challenge response fields)
#
# It does not actually validate the credentials in any way, but attempts to
# verify their consistency from one request to the next, by saving their
# values via a (cookie-based) session.
#
# In order to test with 2FA, the initial 'GET /' request should include
# the query string '?want_2fa=1'.
########################################

import sys
import ssl
import random
import base64
from json import dumps
from functools import wraps
from flask import Flask, request, abort, redirect, url_for, make_response, session

host, port, *cert_and_maybe_keyfile = sys.argv[1:]

context = ssl.SSLContext()
context.load_cert_chain(*cert_and_maybe_keyfile)

app = Flask(__name__)
app.config.update(SECRET_KEY=b'fake', DEBUG=True, HOST=host, PORT=int(port), SESSION_COOKIE_NAME='fake')

########################################

def cookify(jsonable):
    return base64.urlsafe_b64encode(dumps(jsonable).encode())

def require_SVPNCOOKIE(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not request.cookies.get('SVPNCOOKIE'):
            session.clear()
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapped

def check_form_against_session(*fields):
    def inner(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            for f in fields:
                assert session.get(f) == request.form.get(f), \
                    f'at step {session.get("step")}: form {f!r} {request.form.get(f)!r} != session {f!r} {session.get(f)!r}'
            return fn(*args, **kwargs)
        return wrapped
    return inner

########################################

# Respond to initial 'GET /' with a login form
# Respond to initial 'GET /<realm>' with a redirect to '/remote/login?realm=<realm>'
# [Save want_2fa query parameter in the session for use later]
@app.route('/')
@app.route('/<realm>')
def realm(realm=None):
    session.update(step='GET-realm', want_2fa='want_2fa' in request.args)
    # print(session)
    if realm:
        return redirect(url_for('login', realm=realm))
    else:
        return login()


# Respond to 'GET /remote/login?realm=<realm>' with a placeholder stub (since OpenConnect doesn't even try to parse the form)
# [Save realm in the session for verification of client state later]
@app.route('/remote/login')
def login():
    realm = request.args.get('realm')
    session.update(step='GET-login-form', realm=realm or '')
    return f'login page for realm {realm!r}'


# Respond to 'POST /remote/logincheck'
@app.route('/remote/logincheck', methods=['POST'])
def logincheck():
    want_2fa = session.get('want_2fa')

    if (want_2fa and request.form.get('code')):
        return complete_2fa()
    elif (want_2fa and request.form.get('username') and request.form.get('credential')):
        return send_2fa_challenge()
    elif (request.form.get('username') and request.form.get('credential')):
        return complete_non_2fa()
    abort(405)


# 2FA completion: ensure that client has parroted back the same values
# for username, reqid, polid, grp, portal, magic
# [Save code in the session for potential use later]
@check_form_against_session('username', 'reqid', 'polid', 'grp', 'portal', 'magic')
def complete_2fa():
    session.update(step='complete-2FA', code=request.form.get('code'))
    # print(session)

    resp = make_response('ret=1,redir=/remote/fortisslvpn_xml')
    resp.set_cookie('SVPNCOOKIE', cookify(dict(session)))
    return resp


# 2FA initial login: ensure that client has sent the right realm value, and
# reply with a token challenge containing all known fields.
# [Save username, credential, and challenge fields in the session for verification of client state later]
@check_form_against_session('realm')
def send_2fa_challenge():
    session.update(step='send-2FA-challenge', username=request.form.get('username'), credential=request.form.get('credential'),
                   reqid=str(random.randint(10_000_000, 99_000_000)), polid='1-1-'+str(random.randint(10_000_000, 99_000_000)),
                   magic='1-'+str(random.randint(10_000_000, 99_000_000)), portal=random.choice('ABCD'), grp=random.choice('EFGH'))
    # print(session)

    return ('ret=2,reqid={reqid},polid={polid},grp={grp},portal={portal},magic={magic},'
            'tokeninfo=,chal_msg=Please enter your token code'.format(**session),
            {'content-type': 'text/plain'})


# Non-2FA login: ensure that client has sent the right realm value
@check_form_against_session('realm')
def complete_non_2fa():
    session.update(step='complete-non-2FA', username=request.form.get('username'), credential=request.form.get('credential'))
    # print(session)

    resp = make_response('ret=1,redir=/remote/fortisslvpn_xml', {'content-type': 'text/plain'})
    resp.set_cookie('SVPNCOOKIE', cookify(dict(session)))
    return resp


# Respond to 'GET /fortisslvpn with a placeholder stub (since OpenConnect doesn't even try to parse this)
@app.route('/remote/fortisslvpn')
@require_SVPNCOOKIE
def html_config():
    return 'VPN config in HTML format'


# Respond to 'GET /fortisslvpn_xml with a fake config
@app.route('/remote/fortisslvpn_xml')
@require_SVPNCOOKIE
def xml_config():
    return ('''
            <?xml version="1.0" encoding="utf-8"?>
            <sslvpn-tunnel ver="2" dtls="1" patch="1">
              <dtls-config heartbeat-interval="10" heartbeat-fail-count="10" heartbeat-idle-timeout="10" client-hello-timeout="10"/>
              <tunnel-method value="ppp"/>
              <tunnel-method value="tun"/>
              <fos platform="FakeFortigate" major="1" minor="2" patch="3" build="4567" branch="4567"/>
              <ipv4>
                <dns ip="1.1.1.1"/>
                <dns ip="8.8.8.8" domain="foo.com"/>
                <split-dns domains='mydomain1.local,mydomain2.local' dnsserver1='10.10.10.10' dnsserver2='10.10.10.11' />
                <assigned-addr ipv4="10.11.1.123"/>
                <split-tunnel-info>
                  <addr ip="10.11.10.10" mask="255.255.255.255"/>
                  <addr ip="10.11.1.0" mask="255.255.255.0"/>
                </split-tunnel-info>
              </ipv4>
              <idle-timeout val="3600"/>
              <auth-timeout val="18000"/>
            </sslvpn-tunnel>''',
            {'content-type': 'application/xml'})


# Respond to faux-CONNECT 'GET /remote/sslvpn-tunnel' with 403 Forbidden
# (what the real Fortinet server sends when it doesn't like the parameters,
# intended to trigger "cookie rejected" error in OpenConnect)
@app.route('/remote/sslvpn-tunnel')
@require_SVPNCOOKIE
def tunnel():
    abort(403)


# Respond to 'GET /remote/logout' by clearing session and SVPNCOOKIE
@app.route('/remote/logout')
@require_SVPNCOOKIE
def logout():
    session.clear()
    resp = make_response('successful logout')
    resp.set_cookie('SVPNCOOKIE', '')
    return resp


app.run(host=app.config['HOST'], port=app.config['PORT'], debug=app.config['DEBUG'],
        ssl_context=context, use_debugger=False)