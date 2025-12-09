import importlib
import io
import os
import shutil
import sys
import tempfile
import urllib.parse
import unittest
from unittest import mock


class SmartAITestCase(unittest.TestCase):
    def reload_app_module(self):
        sys.modules.pop("app", None)
        sys.modules.pop("backend.app", None)
        sys.modules.pop("backend", None)
        return importlib.import_module("app")

    def setUp(self):
        self.original_env = os.environ.copy()
        self.temp_dir = tempfile.mkdtemp(prefix="smartai-tests-")
        self.db_path = os.path.join(self.temp_dir, "test_app.db")
        self.known_faces_dir = os.path.join(self.temp_dir, "known_faces")
        self.camera_config_path = os.path.join(self.temp_dir, "cameras.json")

        with open(self.camera_config_path, "w", encoding="utf-8") as file_handle:
            file_handle.write(
                """
                {
                  "list": [
                    {"id": "cam-1", "name": "Lobby Cam", "source": "0", "type": "usb", "enabled": true},
                    {"id": "cam-2", "name": "Gate Cam", "source": "1", "type": "usb", "enabled": true}
                  ]
                }
                """
            )

        os.environ["APP_DB_PATH"] = self.db_path
        os.environ["KNOWN_FACES_DIR"] = self.known_faces_dir
        os.environ["CAMERA_CONFIG_PATH"] = self.camera_config_path
        os.environ["DISABLE_CAMERA"] = "1"
        os.environ["DISABLE_YOLO"] = "1"
        os.environ["FLASK_SECRET"] = "test-secret"
        os.environ["GEMINI_API_KEY"] = ""

        self.app_module = self.reload_app_module()
        self.backend_module = importlib.import_module("backend.app")
        self.app_module.app.config.update(TESTING=True)
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        sys.modules.pop("app", None)
        sys.modules.pop("backend.app", None)
        sys.modules.pop("backend", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self.original_env)

    def ajax_post(self, path, payload, client=None):
        target = client or self.client
        return target.post(
            path,
            json=payload,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    def local_voice_post(self, path, payload, client=None):
        target = client or self.client
        return target.post(
            path,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-SmartAI-Voice-Assistant": "1",
            },
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

    def local_voice_get(self, path, client=None):
        target = client or self.client
        return target.get(
            path,
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-SmartAI-Voice-Assistant": "1",
            },
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

    def create_user(self, email="user@example.com", password="password123"):
        response = self.ajax_post(
            "/register",
            {"email": email, "password": password},
        )
        self.assertEqual(response.status_code, 200)
        return response

    def login_user(self, client=None, email="user@example.com", password="password123"):
        target = client or self.client
        response = self.ajax_post(
            "/login",
            {"email": email, "password": password},
            client=target,
        )
        self.assertEqual(response.status_code, 200)
        return response

    def test_first_run_redirects_home_to_register(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/register"))

    def test_first_run_redirects_login_to_register(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/register"))

    def test_login_and_register_pages_render_after_user_exists(self):
        self.create_user()

        logged_out = self.app_module.app.test_client()
        login_page = logged_out.get("/login")
        register_page = logged_out.get("/register")

        self.assertEqual(login_page.status_code, 200)
        self.assertEqual(register_page.status_code, 200)
        self.assertIn(b"Welcome back", login_page.data)
        self.assertIn(b"Create your account", register_page.data)

    def test_home_redirects_to_login_after_user_exists(self):
        self.create_user()

        logged_out = self.app_module.app.test_client()
        response = logged_out.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_protected_api_returns_401_json_when_logged_out(self):
        self.create_user()

        logged_out = self.app_module.app.test_client()
        response = logged_out.get("/health", headers={"Accept": "application/json"})
        payload = response.get_json()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(payload["error"], "authentication required")
        self.assertEqual(payload["redirect"], "/login")

    def test_register_and_login_allow_dashboard_access(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)
        response = logged_in.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Local surveillance dashboard", response.data)
        self.assertIn(b"/static/js/dashboard.js", response.data)

    def test_device_flow_works_after_login(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        devices_response = logged_in.get("/api/devices", headers={"Accept": "application/json"})
        self.assertEqual(devices_response.status_code, 200)

        add_response = self.ajax_post(
            "/api/devices",
            {"name": "desk lamp"},
            client=logged_in,
        )
        self.assertEqual(add_response.status_code, 200)
        self.assertEqual(add_response.get_json()["name"], "desk lamp")

        toggle_response = logged_in.post(
            "/toggle/desk%20lamp",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(toggle_response.status_code, 200)
        self.assertEqual(toggle_response.get_json()["desk lamp"], "ON")

    def test_add_camera_by_ip(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post(
            "/api/cameras",
            {"name": "ESP Cam", "source": "192.168.1.50"},
            client=logged_in,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload["camera"]["name"], "ESP Cam")
        self.assertIn("http://192.168.1.50", payload["camera"]["source"])
        self.assertEqual(toggle_response.status_code, 200)
        self.assertEqual(toggle_response.get_json()["desk lamp"], "ON")

    def test_camera_feed_falls_back_when_disabled(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = logged_in.get("/camera_feed")
        self.assertEqual(response.status_code, 200)
        first_chunk = next(response.response)
        self.assertTrue(first_chunk.startswith(b"--frame"))
        response.close()

    def test_assistant_returns_clear_message_without_gemini(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post(
            "/assistant",
            {"query": "hello"},
            client=logged_in,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Gemini AI not configured", response.get_json()["reply"])

    def test_voice_assistant_prefers_hindi_fallback_without_gemini(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post(
            "/assistant",
            {
                "query": "hello",
                "source": "voice",
                "preferred_language": "hi-IN",
            },
            client=logged_in,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Gemini AI configure nahi hai", response.get_json()["reply"])

    def test_voice_assistant_returns_hindi_time_response(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post(
            "/assistant",
            {
                "query": "samay kya hai",
                "source": "voice",
                "preferred_language": "hi-IN",
            },
            client=logged_in,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Abhi samay", response.get_json()["reply"])

    def test_local_voice_assistant_can_access_protected_routes_without_login(self):
        assistant_response = self.local_voice_post(
            "/assistant",
            {
                "query": "samay kya hai",
                "source": "voice",
                "preferred_language": "hi-IN",
            },
        )
        alert_response = self.local_voice_get("/get_alert")
        behavior_response = self.local_voice_get("/behavior")

        self.assertEqual(assistant_response.status_code, 200)
        self.assertIn("Abhi samay", assistant_response.get_json()["reply"])
        self.assertEqual(alert_response.status_code, 200)
        self.assertIn("face_recognition_enabled", alert_response.get_json())
        self.assertEqual(behavior_response.status_code, 200)
        self.assertIn("person_present", behavior_response.get_json())

    def test_face_recognition_init_survives_missing_directory(self):
        self.assertFalse(self.backend_module.FACE_RECOGNITION_ENABLED)
        self.assertIn("No known faces directory", self.backend_module.face_state["message"])

    def test_health_reports_face_recognition_disabled_without_training_data(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)
        response = logged_in.get("/health", headers={"Accept": "application/json"})
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["face_recognition_enabled"])
        self.assertIn("No known faces directory", payload["face_recognition_status"])

    def test_get_alert_includes_face_recognition_fields(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)
        response = logged_in.get("/get_alert", headers={"Accept": "application/json"})
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertIn("face_recognition_enabled", payload)
        self.assertIn("faces", payload)
        self.assertEqual(payload["faces"], [])
        self.assertFalse(payload["face_recognition_enabled"])

    def test_face_recognition_can_initialize_when_yolo_disabled(self):
        os.makedirs(self.known_faces_dir, exist_ok=True)
        sample = self.backend_module.np.zeros(self.backend_module.FACE_IMAGE_SIZE, dtype="uint8")
        recognizer = mock.Mock()

        with (
            mock.patch.object(
                self.backend_module,
                "build_face_detector",
                return_value=(mock.Mock(), None),
            ),
            mock.patch.object(
                self.backend_module,
                "load_known_face_training_data",
                return_value=(
                    [sample],
                    [0],
                    {0: "Akash"},
                    {"loaded": 1, "people": 1, "skipped": self.backend_module.Counter()},
                ),
            ),
            mock.patch.object(
                self.backend_module.cv2.face,
                "LBPHFaceRecognizer_create",
                return_value=recognizer,
            ),
        ):
            self.backend_module.init_face_recognition()

        self.assertFalse(self.backend_module.YOLO_ENABLED)
        self.assertTrue(self.backend_module.FACE_RECOGNITION_ENABLED)
        self.assertEqual(self.backend_module.face_state["known_people"], 1)
        recognizer.train.assert_called_once()

    def test_load_face_sample_from_file_accepts_single_face(self):
        detector = mock.Mock()
        detector.detectMultiScale.return_value = [(0, 0, 32, 32)]
        image = self.backend_module.np.zeros((80, 80, 3), dtype="uint8")

        with mock.patch.object(self.backend_module.cv2, "imread", return_value=image):
            sample, reason = self.backend_module.load_face_sample_from_file(
                "alice.png",
                detector,
            )

        self.assertIsNone(reason)
        self.assertIsNotNone(sample)
        self.assertEqual(sample.shape, self.backend_module.FACE_IMAGE_SIZE)

    def test_load_face_sample_from_file_skips_zero_face_images(self):
        detector = mock.Mock()
        detector.detectMultiScale.return_value = ()
        image = self.backend_module.np.zeros((80, 80, 3), dtype="uint8")

        with mock.patch.object(self.backend_module.cv2, "imread", return_value=image):
            sample, reason = self.backend_module.load_face_sample_from_file(
                "empty.png",
                detector,
            )

        self.assertIsNone(sample)
        self.assertEqual(reason, "no_face")

    def test_load_face_sample_from_file_skips_multi_face_images(self):
        detector = mock.Mock()
        detector.detectMultiScale.return_value = [(0, 0, 32, 32), (40, 40, 32, 32)]
        image = self.backend_module.np.zeros((80, 80, 3), dtype="uint8")

        with mock.patch.object(self.backend_module.cv2, "imread", return_value=image):
            sample, reason = self.backend_module.load_face_sample_from_file(
                "crowd.png",
                detector,
            )

        self.assertIsNone(sample)
        self.assertEqual(reason, "multiple_faces")

    def test_predict_face_match_recognizes_known_person_within_threshold(self):
        sample = self.backend_module.np.zeros(self.backend_module.FACE_IMAGE_SIZE, dtype="uint8")
        recognizer = mock.Mock()
        recognizer.predict.return_value = (0, 42.0)

        result = self.backend_module.predict_face_match(
            sample,
            recognizer=recognizer,
            label_map={0: "Akash"},
            threshold=70,
        )

        self.assertTrue(result["recognized"])
        self.assertEqual(result["name"], "Akash")

    def test_predict_face_match_returns_unknown_above_threshold(self):
        sample = self.backend_module.np.zeros(self.backend_module.FACE_IMAGE_SIZE, dtype="uint8")
        recognizer = mock.Mock()
        recognizer.predict.return_value = (0, 88.0)

        result = self.backend_module.predict_face_match(
            sample,
            recognizer=recognizer,
            label_map={0: "Akash"},
            threshold=70,
        )

        self.assertFalse(result["recognized"])
        self.assertEqual(result["name"], "Unknown")

    def test_added_devices_can_toggle_after_backend_reload(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        add_response = self.ajax_post(
            "/api/devices",
            {"name": "desk lamp"},
            client=logged_in,
        )
        self.assertEqual(add_response.status_code, 200)

        self.app_module = self.reload_app_module()
        self.backend_module = importlib.import_module("backend.app")
        self.app_module.app.config.update(TESTING=True)

        reloaded_client = self.app_module.app.test_client()
        self.login_user(client=reloaded_client)

        devices_response = reloaded_client.get("/api/devices", headers={"Accept": "application/json"})
        device_names = [item["name"] for item in devices_response.get_json()["devices"]]
        self.assertIn("desk lamp", device_names)

        toggle_response = reloaded_client.post(
            "/toggle/desk%20lamp",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(toggle_response.status_code, 200)
        self.assertEqual(toggle_response.get_json()["desk lamp"], "ON")

    def test_first_user_becomes_admin_and_can_read_admin_summary(self):
        self.create_user("admin@example.com")

        response = self.client.get("/api/admin/summary", headers={"Accept": "application/json"})
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["current_user_role"], "admin")
        self.assertEqual(payload["user_count"], 1)
        self.assertEqual(payload["admin_count"], 1)
        self.assertEqual(payload["users"][0]["role"], "admin")

    def test_non_admin_cannot_access_admin_summary(self):
        self.create_user("admin@example.com")

        member_client = self.app_module.app.test_client()
        member_response = self.ajax_post(
            "/register",
            {"email": "member@example.com", "password": "password123"},
            client=member_client,
        )
        self.assertEqual(member_response.status_code, 200)

        response = member_client.get("/api/admin/summary", headers={"Accept": "application/json"})
        payload = response.get_json()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(payload["error"], "admin access required")

    def test_admin_can_update_user_role(self):
        self.create_user("admin@example.com")

        member_client = self.app_module.app.test_client()
        member_response = self.ajax_post(
            "/register",
            {"email": "member@example.com", "password": "password123"},
            client=member_client,
        )
        self.assertEqual(member_response.status_code, 200)

        response = self.ajax_post(
            f"/api/admin/users/{urllib.parse.quote('member@example.com', safe='')}/role",
            {"role": "admin"},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["user"]["role"], "admin")

    def test_profile_api_returns_full_name_and_role(self):
        response = self.ajax_post(
            "/register",
            {
                "full_name": "Akash Sharma",
                "email": "akash@example.com",
                "password": "password123",
            },
        )
        self.assertEqual(response.status_code, 200)

        profile_response = self.client.get("/api/profile", headers={"Accept": "application/json"})
        profile = profile_response.get_json()["profile"]

        self.assertEqual(profile_response.status_code, 200)
        self.assertEqual(profile["full_name"], "Akash Sharma")
        self.assertEqual(profile["display_name"], "Akash Sharma")
        self.assertEqual(profile["role"], "admin")

    def test_device_can_be_deleted(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        add_response = self.ajax_post(
            "/api/devices",
            {"name": "desk lamp"},
            client=logged_in,
        )
        self.assertEqual(add_response.status_code, 200)

        delete_response = logged_in.delete(
            "/api/devices/desk%20lamp",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.get_json()["name"], "desk lamp")

        devices_response = logged_in.get("/api/devices", headers={"Accept": "application/json"})
        names = [item["name"] for item in devices_response.get_json()["devices"]]
        self.assertNotIn("desk lamp", names)

    def test_assistant_manual_mode_can_toggle_device(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post(
            "/assistant",
            {"query": "turn on light", "mode": "manual"},
            client=logged_in,
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["handled_locally"])
        self.assertEqual(payload["mode"], "manual")
        self.assertIn("light", payload["reply"].lower())
        self.assertEqual(payload["actions"][0]["state"], "ON")

    def test_assistant_research_mode_returns_local_summary_without_gemini(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post(
            "/assistant",
            {"query": "give me a research update", "mode": "research"},
            client=logged_in,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Research mode summary", response.get_json()["reply"])

    def test_get_alert_includes_camera_diagnostics(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = logged_in.get("/get_alert", headers={"Accept": "application/json"})
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertIn("camera_diagnostics", payload)
        self.assertIn("yolo_status", payload["camera_diagnostics"])

    def test_camera_registry_lists_profiles_and_switches_active_camera(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        list_response = logged_in.get("/api/cameras", headers={"Accept": "application/json"})
        list_payload = list_response.get_json()

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_payload["camera_count"], 2)
        self.assertEqual(list_payload["active_camera"]["id"], "cam-1")

        switch_response = self.ajax_post(
            "/api/cameras/active",
            {"camera_id": "cam-2"},
            client=logged_in,
        )
        switch_payload = switch_response.get_json()

        self.assertEqual(switch_response.status_code, 200)
        self.assertTrue(switch_payload["ok"])
        self.assertEqual(switch_payload["active_camera"]["id"], "cam-2")

    def test_face_registry_upload_saves_known_face_samples(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        with (
            mock.patch.object(self.backend_module, "build_face_detector", return_value=(mock.Mock(), None)),
            mock.patch.object(
                self.backend_module,
                "decode_uploaded_face_image",
                return_value=(mock.Mock(), None, b"fake-image-bytes"),
            ),
            mock.patch.object(
                self.backend_module,
                "load_face_sample_from_image",
                return_value=(mock.Mock(), None),
            ),
            mock.patch.object(self.backend_module, "init_face_recognition"),
        ):
            response = logged_in.post(
                "/api/faces",
                data={
                    "name": "Akash",
                    "images": (io.BytesIO(b"not-a-real-image"), "akash.jpg"),
                },
                content_type="multipart/form-data",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        payload = response.get_json()
        person_dir = os.path.join(self.known_faces_dir, "Akash")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["person"], "Akash")
        self.assertEqual(len(payload["saved_files"]), 1)
        self.assertTrue(os.path.isdir(person_dir))
        self.assertTrue(os.path.exists(os.path.join(person_dir, payload["saved_files"][0])))

    def test_assistant_manual_mode_can_switch_camera(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post(
            "/assistant",
            {"query": "switch to next camera", "mode": "manual"},
            client=logged_in,
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["handled_locally"])
        self.assertEqual(payload["actions"][0]["type"], "camera")
        self.assertEqual(self.backend_module.get_active_camera_profile()["id"], "cam-2")


if __name__ == "__main__":
    unittest.main()
