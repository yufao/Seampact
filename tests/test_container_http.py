import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from seampact.server import make_server
from seampact.service import Platform


class ContainerHTTPTests(unittest.TestCase):
    def test_wildcard_requires_auth_and_published_port_preserves_origin_checks(self):
        with tempfile.TemporaryDirectory() as root:
            platform=Platform(root)
            with self.assertRaises(ValueError):
                make_server(platform,0,require_session=False,host="0.0.0.0")
            server=make_server(platform,0,host="0.0.0.0",public_port=8875)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base=f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(base+"/health") as response:
                    health=json.load(response)
                self.assertTrue(health["session_required"])
                self.assertNotIn("profiles",health)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base+"/api/state")
                self.assertEqual(error.exception.code,403)
                opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                login=urllib.request.Request(base+"/?access="+server.access_token,headers={"Host":"127.0.0.1:8875"})
                with opener.open(login) as response:self.assertEqual(response.status,200)
                request=urllib.request.Request(base+"/api/state",headers={"Host":"127.0.0.1:8875","Origin":"http://127.0.0.1:8875"})
                with opener.open(request) as response:self.assertIn("version",json.load(response))
                for headers in ({"Host":"outside.example:8875"},{"Host":"127.0.0.1:8875","Origin":"https://outside.example"}):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        opener.open(urllib.request.Request(base+"/api/state",headers=headers))
                    self.assertEqual(error.exception.code,403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                platform.close()
