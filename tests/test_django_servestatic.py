from __future__ import annotations

import asyncio
import html
import shutil
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urljoin, urlparse

import brotli
import django
import pytest
from asgiref.testing import ApplicationCommunicator
from django.conf import settings
from django.contrib.staticfiles import finders, storage
from django.core.asgi import get_asgi_application
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application
from django.templatetags.static import static
from django.test.utils import override_settings
from django.utils.functional import empty

from servestatic.middleware import AsyncServeStaticFileResponse, ServeStaticMiddleware
from servestatic.utils import AsyncFile

from .utils import (
    AppServer,
    AsgiAppServer,
    AsgiHttpScopeEmulator,
    AsgiReceiveEmulator,
    AsgiSendEmulator,
    Files,
)


def reset_lazy_object(obj):
    obj._wrapped = empty


def get_url_path(base, url):
    return urlparse(urljoin(base, url)).path


@pytest.fixture
def static_files():
    files = Files("static", js="app.js", nonascii="nonascii\u2713.txt", txt="large-file.txt")
    with override_settings(STATICFILES_DIRS=[files.directory]):
        yield files


@pytest.fixture
def root_files():
    files = Files("root", robots="robots.txt")
    with override_settings(SERVESTATIC_ROOT=files.directory):
        yield files


@pytest.fixture
def tmp():
    tmp_dir = tempfile.mkdtemp()
    with override_settings(STATIC_ROOT=tmp_dir):
        yield tmp_dir
    shutil.rmtree(tmp_dir)


@pytest.fixture
def _collect_static(static_files, root_files, tmp):
    reset_lazy_object(storage.staticfiles_storage)
    call_command("collectstatic", verbosity=0, interactive=False)


@pytest.fixture
def application(_collect_static):
    return get_wsgi_application()


@pytest.fixture
def asgi_application(_collect_static):
    return get_asgi_application()


@pytest.fixture
def server(application):
    app_server = AppServer(application)
    with closing(app_server):
        yield app_server


@pytest.mark.usefixtures("_collect_static")
def test_get_root_file(server, root_files):
    response = server.get(root_files.robots_url)
    assert response.content == root_files.robots_content


@override_settings(SERVESTATIC_USE_MANIFEST=False)
@pytest.mark.usefixtures("_collect_static")
def test_get_root_file_no_manifest(server, root_files):
    response = server.get(root_files.robots_url)
    assert response.content == root_files.robots_content


@pytest.mark.usefixtures("_collect_static")
def test_versioned_file_cached_forever(server, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    response = server.get(url)
    assert response.content == static_files.js_content
    assert response.headers.get("Cache-Control") == f"max-age={ServeStaticMiddleware.FOREVER}, public, immutable"


@pytest.mark.skipif(django.VERSION >= (5, 0), reason="Django <5.0 only")
@pytest.mark.usefixtures("_collect_static")
def test_asgi_versioned_file_cached_forever_brotli(asgi_application, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    scope = AsgiHttpScopeEmulator({"path": url, "headers": [(b"accept-encoding", b"br")]})
    receive = AsgiReceiveEmulator()
    send = AsgiSendEmulator()
    asyncio.run(AsgiAppServer(asgi_application)(scope, receive, send))
    assert brotli.decompress(send.body) == static_files.js_content
    assert (
        send.headers.get(b"Cache-Control", b"").decode("utf-8")
        == f"max-age={ServeStaticMiddleware.FOREVER}, public, immutable"
    )
    assert send.headers.get(b"Content-Encoding") == b"br"
    assert send.headers.get(b"Vary") == b"Accept-Encoding"


@pytest.mark.skipif(django.VERSION < (5, 0), reason="Django 5.0+ only")
@pytest.mark.usefixtures("_collect_static")
def test_asgi_versioned_file_cached_forever_brotli_2(asgi_application, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    scope = AsgiHttpScopeEmulator({"path": url, "headers": [(b"accept-encoding", b"br")]})

    async def executor():
        communicator = ApplicationCommunicator(asgi_application, scope)
        await communicator.send_input(scope)
        response_start = await communicator.receive_output()
        response_body = await communicator.receive_output()
        return response_start | response_body

    response = asyncio.run(executor())
    headers = dict(response["headers"])

    assert brotli.decompress(response["body"]) == static_files.js_content
    assert (
        headers.get(b"Cache-Control", b"").decode("utf-8")
        == f"max-age={ServeStaticMiddleware.FOREVER}, public, immutable"
    )
    assert headers.get(b"Content-Encoding") == b"br"
    assert headers.get(b"Vary") == b"Accept-Encoding"


@pytest.mark.usefixtures("_collect_static")
def test_unversioned_file_not_cached_forever(server, static_files):
    url = settings.STATIC_URL + static_files.js_path
    response = server.get(url)
    assert response.content == static_files.js_content
    assert response.headers.get("Cache-Control") == "max-age=60, public"


@pytest.mark.usefixtures("_collect_static")
def test_get_gzip(server, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    response = server.get(url, headers={"Accept-Encoding": "gzip"})
    assert response.content == static_files.js_content
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["Vary"] == "Accept-Encoding"


@pytest.mark.usefixtures("_collect_static")
def test_get_brotli(server, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    response = server.get(url, headers={"Accept-Encoding": "gzip, br"})
    assert response.content == static_files.js_content
    assert response.headers["Content-Encoding"] == "br"
    assert response.headers["Vary"] == "Accept-Encoding"


@pytest.mark.usefixtures("_collect_static")
def test_no_content_type_when_not_modified(server, static_files):
    last_mod = "Fri, 11 Apr 2100 11:47:06 GMT"
    url = settings.STATIC_URL + static_files.js_path
    response = server.get(url, headers={"If-Modified-Since": last_mod})
    assert "Content-Type" not in response.headers


@pytest.mark.usefixtures("_collect_static")
def test_get_nonascii_file(server, static_files):
    url = settings.STATIC_URL + static_files.nonascii_path
    response = server.get(url)
    assert response.content == static_files.nonascii_content


@pytest.fixture(params=[True, False])
def finder_static_files(request):
    files = Files("static", js="app.js", index="with-index/index.html")
    with override_settings(
        STATICFILES_DIRS=[files.directory],
        SERVESTATIC_USE_FINDERS=True,
        SERVESTATIC_AUTOREFRESH=request.param,
        SERVESTATIC_INDEX_FILE=True,
        STATIC_ROOT=None,
    ):
        finders.get_finder.cache_clear()
        yield files


@pytest.mark.usefixtures("_collect_static")
def test_no_content_disposition_header(server, static_files):
    url = settings.STATIC_URL + static_files.js_path
    response = server.get(url)
    assert response.headers.get("content-disposition") is None


@pytest.fixture
def finder_application(finder_static_files, application):
    return application


@pytest.fixture
def finder_server(finder_application):
    app_server = AppServer(finder_application)
    with closing(app_server):
        yield app_server


def test_file_served_from_static_dir(finder_static_files, finder_server):
    url = settings.STATIC_URL + finder_static_files.js_path
    response = finder_server.get(url)
    assert response.content == finder_static_files.js_content


@override_settings(SERVESTATIC_USE_MANIFEST=False)
def test_file_served_from_static_dir_no_manifest(finder_static_files, finder_server):
    url = settings.STATIC_URL + finder_static_files.js_path
    response = finder_server.get(url)
    assert response.content == finder_static_files.js_content


def test_non_ascii_requests_safely_ignored(finder_server):
    response = finder_server.get(settings.STATIC_URL + "test\u263a")
    assert response.status_code == 404


def test_requests_for_directory_safely_ignored(finder_server):
    url = f"{settings.STATIC_URL}directory"
    response = finder_server.get(url)
    assert response.status_code == 404


def test_index_file_served_at_directory_path(finder_static_files, finder_server):
    path = finder_static_files.index_path.rpartition("/")[0] + "/"
    response = finder_server.get(settings.STATIC_URL + path)
    assert response.content == finder_static_files.index_content


@override_settings(SERVESTATIC_USE_MANIFEST=False)
def test_index_file_served_at_directory_path_no_manifest(finder_static_files, finder_server):
    path = finder_static_files.index_path.rpartition("/")[0] + "/"
    response = finder_server.get(settings.STATIC_URL + path)
    assert response.content == finder_static_files.index_content


def test_index_file_path_redirected(finder_static_files, finder_server):
    directory_path = finder_static_files.index_path.rpartition("/")[0] + "/"
    index_url = settings.STATIC_URL + finder_static_files.index_path
    response = finder_server.get(index_url, allow_redirects=False)
    location = get_url_path(response.url, response.headers["Location"])
    assert response.status_code == 302
    assert location == settings.STATIC_URL + directory_path


def test_directory_path_without_trailing_slash_redirected(finder_static_files, finder_server):
    directory_path = finder_static_files.index_path.rpartition("/")[0] + "/"
    directory_url = settings.STATIC_URL + directory_path.rstrip("/")
    response = finder_server.get(directory_url, allow_redirects=False)
    location = get_url_path(response.url, response.headers["Location"])
    assert response.status_code == 302
    assert location == settings.STATIC_URL + directory_path


def test_servestatic_file_response_has_only_one_header():
    response = AsyncServeStaticFileResponse(AsyncFile(__file__, "rb"))
    response.close()
    headers = {key.lower() for key, value in response.items()}
    # This subclass should have none of the default headers that FileReponse sets
    assert headers == {"content-type"}


@override_settings(STATIC_URL="static/")
@pytest.mark.usefixtures("_collect_static")
def test_relative_static_url(server, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    response = server.get(url)
    assert response.content == static_files.js_content


def test_404_in_prod(server):
    response = server.get(f"{settings.STATIC_URL}garbage")
    response_content = str(response.content.decode())
    response_content = html.unescape(response_content)

    assert response.status_code == 404
    assert "ServeStatic did not find the file 'garbage' within the following paths:" not in response_content


@override_settings(DEBUG=True)
def test_error_message(server):
    response = server.get(f"{settings.STATIC_URL}garbage")
    response_content = str(response.content.decode())
    response_content = html.unescape(response_content)

    # Beautify for easier debugging
    response_content = response_content[response_content.index("ServeStatic") :]

    assert "ServeStatic did not find the file 'garbage' within the following paths:" in response_content
    assert "•" in response_content
    assert str(Path(__file__).parent / "test_files" / "static") in response_content


@override_settings(FORCE_SCRIPT_NAME="/subdir", STATIC_URL="static/")
@pytest.mark.usefixtures("_collect_static")
def test_force_script_name(server, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    assert url.startswith("/subdir/static/")
    response = server.get(url)
    assert "/subdir" in response.url
    assert response.content == static_files.js_content


@override_settings(FORCE_SCRIPT_NAME="/subdir", STATIC_URL="/subdir/static/")
@pytest.mark.usefixtures("_collect_static")
def test_force_script_name_with_matching_static_url(server, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    assert url.startswith("/subdir/static/")
    response = server.get(url)
    assert "/subdir" in response.url
    assert response.content == static_files.js_content


@pytest.mark.usefixtures("_collect_static")
def test_range_response(server, static_files):
    ...
    # FIXME: This test is not working, seemingly due to bugs with AppServer.

    # url = storage.staticfiles_storage.url(static_files.js_path)
    # response = server.get(url, headers={"Range": "bytes=0-13"})
    # assert response.content == static_files.js_content[:14]
    # assert response.status_code == 206
    # assert (
    #     response.headers["Content-Range"]
    #     == f"bytes 0-13/{len(static_files.js_content)}"
    # )
    # assert response.headers["Content-Length"] == "14"


@pytest.mark.skipif(django.VERSION >= (5, 0), reason="Django <5.0 only")
@pytest.mark.usefixtures("_collect_static")
def test_asgi_range_response(asgi_application, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    scope = AsgiHttpScopeEmulator({"path": url, "headers": [(b"range", b"bytes=0-13")]})
    receive = AsgiReceiveEmulator()
    send = AsgiSendEmulator()
    asyncio.run(AsgiAppServer(asgi_application)(scope, receive, send))
    assert send.body == static_files.js_content[:14]
    assert send.headers[b"Content-Range"] == b"bytes 0-13/" + str(len(static_files.js_content)).encode()
    assert send.headers[b"Content-Length"] == b"14"
    assert send.status == 206


@pytest.mark.skipif(django.VERSION < (5, 0), reason="Django 5.0+ only")
@pytest.mark.usefixtures("_collect_static")
def test_asgi_range_response_2(asgi_application, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    scope = AsgiHttpScopeEmulator({"path": url, "headers": [(b"range", b"bytes=0-13")]})

    async def executor():
        communicator = ApplicationCommunicator(asgi_application, scope)
        await communicator.send_input(scope)
        response_start = await communicator.receive_output()
        response_body = await communicator.receive_output()
        return response_start | response_body

    response = asyncio.run(executor())
    headers = dict(response["headers"])

    assert response["body"] == static_files.js_content[:14]
    assert headers[b"Content-Range"] == b"bytes 0-13/" + str(len(static_files.js_content)).encode()
    assert headers[b"Content-Length"] == b"14"
    assert response["status"] == 206


@pytest.mark.usefixtures("_collect_static")
def test_out_of_range_error(server, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    response = server.get(url, headers={"Range": "bytes=900-999"})
    assert response.status_code == 416


@pytest.mark.skipif(django.VERSION >= (5, 0), reason="Django <5.0 only")
@pytest.mark.usefixtures("_collect_static")
def test_asgi_out_of_range_error(asgi_application, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    scope = AsgiHttpScopeEmulator({"path": url, "headers": [(b"range", b"bytes=900-999")]})
    receive = AsgiReceiveEmulator()
    send = AsgiSendEmulator()
    asyncio.run(AsgiAppServer(asgi_application)(scope, receive, send))
    assert send.status == 416


@pytest.mark.skipif(django.VERSION < (5, 0), reason="Django 5.0+ only")
@pytest.mark.usefixtures("_collect_static")
def test_asgi_out_of_range_error_2(asgi_application, static_files):
    url = storage.staticfiles_storage.url(static_files.js_path)
    scope = AsgiHttpScopeEmulator({"path": url, "headers": [(b"range", b"bytes=900-999")]})

    async def executor():
        communicator = ApplicationCommunicator(asgi_application, scope)
        await communicator.send_input(scope)
        response_start = await communicator.receive_output()
        response_body = await communicator.receive_output()
        return response_start | response_body

    response = asyncio.run(executor())
    assert response["status"] == 416
    assert dict(response["headers"])[b"Content-Range"] == b"bytes */%d" % len(static_files.js_content)


@pytest.mark.skipif(django.VERSION >= (5, 0), reason="Django <5.0 only")
@pytest.mark.usefixtures("_collect_static")
def test_large_static_file(asgi_application, static_files):
    url = storage.staticfiles_storage.url(static_files.txt_path)
    scope = AsgiHttpScopeEmulator({"path": url, "headers": []})
    receive = AsgiReceiveEmulator()
    send = AsgiSendEmulator()
    asyncio.run(AsgiAppServer(asgi_application)(scope, receive, send))
    assert len(send.body) == len(static_files.txt_content)
    assert len(send.body) == 10001
    assert send.body == static_files.txt_content
    assert send.body_count == 2
    assert send.headers[b"Content-Length"] == str(len(static_files.txt_content)).encode()
    assert b"text/plain" in send.headers[b"Content-Type"]


@pytest.mark.skipif(django.VERSION < (5, 0), reason="Django 5.0+ only")
@pytest.mark.usefixtures("_collect_static")
def test_large_static_file_2(asgi_application, static_files):
    url = storage.staticfiles_storage.url(static_files.txt_path)
    scope = AsgiHttpScopeEmulator({"path": url, "headers": []})

    async def executor():
        communicator = ApplicationCommunicator(asgi_application, scope)
        await communicator.send_input(scope)
        response_start = await communicator.receive_output()
        response_body = await communicator.receive_output()
        assert response_body["more_body"] is True
        response_body_2 = await communicator.receive_output()
        response_body["body"] += response_body_2["body"]
        return response_start | response_body

    response = asyncio.run(executor())
    headers = dict(response["headers"])

    assert len(response["body"]) == len(static_files.txt_content)
    assert len(response["body"]) == 10001
    assert response["body"] == static_files.txt_content
    assert headers[b"Content-Length"] == str(len(static_files.txt_content)).encode()
    assert b"text/plain" in headers[b"Content-Type"]


@pytest.mark.skipif(django.VERSION >= (5, 0), reason="Django <5.0 only")
@pytest.mark.usefixtures("static_files")
def test_manifest_with_keep_only_hashed(static_files):
    with override_settings(SERVESTATIC_USE_MANIFEST=True, SERVESTATIC_KEEP_ONLY_HASHED_FILES=True):
        try:
            # Collect static files
            reset_lazy_object(storage.staticfiles_storage)
            call_command("collectstatic", verbosity=0, interactive=False)

            # Determine static URLs
            hashed_path = static("app.js")
            original_path = hashed_path.rsplit("/", 1)[0] + "/app.js"
            assert not hashed_path.endswith("app.js")

            # Check if SERVESTATIC_KEEP_ONLY_HASHED_FILES removed the original file
            scope = AsgiHttpScopeEmulator({"path": original_path, "headers": []})
            receive = AsgiReceiveEmulator()
            send = AsgiSendEmulator()
            asyncio.run(AsgiAppServer(get_asgi_application())(scope, receive, send))
            assert send.status == 404

            # Check if the hashed file can be served
            scope = AsgiHttpScopeEmulator({"path": hashed_path, "headers": []})
            receive = AsgiReceiveEmulator()
            send = AsgiSendEmulator()
            asyncio.run(AsgiAppServer(get_asgi_application())(scope, receive, send))
            assert send.status == 200

        finally:
            static_root: Path = settings.STATIC_ROOT
            shutil.rmtree(static_root, ignore_errors=True)


@pytest.mark.skipif(django.VERSION < (5, 0), reason="Django 5.0+ only")
@pytest.mark.usefixtures("static_files")
def test_manifest_with_keep_only_hashed_2():
    with override_settings(SERVESTATIC_USE_MANIFEST=True, SERVESTATIC_KEEP_ONLY_HASHED_FILES=True):
        try:
            # Collect static files
            reset_lazy_object(storage.staticfiles_storage)
            call_command("collectstatic", verbosity=0, interactive=False)

            # Determine static URLs
            hashed_path = static("app.js")
            original_path = hashed_path.rsplit("/", 1)[0] + "/app.js"
            assert not hashed_path.endswith("app.js")

            # Check if SERVESTATIC_KEEP_ONLY_HASHED_FILES removed the original file
            async def executor():
                scope = AsgiHttpScopeEmulator({"path": original_path, "headers": []})
                communicator = ApplicationCommunicator(get_asgi_application(), scope)
                await communicator.send_input(scope)
                response_start = await communicator.receive_output()
                response_body = await communicator.receive_output()
                return response_start | response_body

            response = asyncio.run(executor())
            assert response["status"] == 404

            # Check if the hashed file can be served
            async def executor_2():
                scope = AsgiHttpScopeEmulator({"path": hashed_path, "headers": []})
                communicator = ApplicationCommunicator(get_asgi_application(), scope)
                await communicator.send_input(scope)
                response_start = await communicator.receive_output()
                response_body = await communicator.receive_output()
                return response_start | response_body

            response = asyncio.run(executor_2())
            assert response["status"] == 200

        finally:
            static_root: Path = settings.STATIC_ROOT
            shutil.rmtree(static_root, ignore_errors=True)
