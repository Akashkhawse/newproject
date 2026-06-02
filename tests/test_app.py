import importlib
import io
import base64
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

    def test_about_page_is_public_and_explains_operational_workflow(self):
        response = self.client.get("/about")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"How it works", response.data)
        self.assertIn(b"Connect", response.data)
        self.assertIn(b"Review", response.data)

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
        self.assertIn(b"Smart Surveillance System AI Powered", response.data)
        self.assertIn(b"/static/js/dashboard_new.js", response.data)

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

    def test_camera_alias_route_adds_camera(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post(
            "/api/camera",
            {"name": "Hallway Cam", "source": "192.168.1.55"},
            client=logged_in,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload["camera"]["name"], "Hallway Cam")
        self.assertEqual(payload["camera_count"], 3)

    def test_mobile_camera_link_registers_profile_and_activates(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post(
            "/api/mobile-camera/link",
            {"device_id": "pixel-7", "set_active": True},
            client=logged_in,
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload["active_camera"]["source"], "mobile://pixel-7")
        self.assertIn("/mobile-camera?", payload["mobile_page_url"])
        self.assertIn("/api/mobile-camera/frame?", payload["mobile_upload_url"])

    def test_mobile_camera_frame_upload_accepts_data_url(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)
        self.ajax_post(
            "/api/mobile-camera/link",
            {"device_id": "default", "set_active": True},
            client=logged_in,
        )

        frame_b64 = base64.b64encode(self.backend_module.EMPTY_FRAME_JPEG).decode("ascii")
        response = logged_in.post(
            "/api/mobile-camera/frame",
            json={"device_id": "default", "image": f"data:image/jpeg;base64,{frame_b64}"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload["device_id"], "default")
        self.assertGreaterEqual(payload["frame_count"], 1)

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

    def test_non_admin_cannot_read_or_search_global_activity(self):
        self.create_user("admin@example.com")

        member_client = self.app_module.app.test_client()
        self.ajax_post(
            "/register",
            {"email": "member@example.com", "password": "password123"},
            client=member_client,
        )

        log_response = member_client.get("/api/event-logs", headers={"Accept": "application/json"})
        report_response = member_client.get("/api/reports/event-logs.csv", headers={"Accept": "application/json"})
        search_response = member_client.get("/api/search?q=admin", headers={"Accept": "application/json"})

        self.assertEqual(log_response.status_code, 403)
        self.assertEqual(report_response.status_code, 403)
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(search_response.get_json()["results"]["activity"], [])

    def test_demoted_admin_session_loses_admin_access_immediately(self):
        self.create_user("admin@example.com")

        member_client = self.app_module.app.test_client()
        self.ajax_post(
            "/register",
            {"email": "member@example.com", "password": "password123"},
            client=member_client,
        )
        promote_response = self.ajax_post(
            f"/api/admin/users/{urllib.parse.quote('member@example.com', safe='')}/role",
            {"role": "admin"},
        )
        self.assertEqual(promote_response.status_code, 200)

        demote_response = self.ajax_post(
            f"/api/admin/users/{urllib.parse.quote('admin@example.com', safe='')}/role",
            {"role": "user"},
            client=member_client,
        )
        self.assertEqual(demote_response.status_code, 200)

        response = self.client.get("/api/admin/summary", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 403)

    def test_revoked_admin_session_cannot_access_admin_routes(self):
        self.create_user("admin@example.com")
        self.backend_module.revoke_sessions_db("admin@example.com")

        response = self.client.get("/api/admin/summary", headers={"Accept": "application/json"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "authentication required")

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

    def test_notification_cannot_be_marked_read_by_another_user(self):
        self.create_user("admin@example.com")

        member_client = self.app_module.app.test_client()
        self.ajax_post(
            "/register",
            {"email": "member@example.com", "password": "password123"},
            client=member_client,
        )
        notification_id = self.backend_module.create_notification(
            "Private member notice",
            user_email="member@example.com",
        )

        admin_response = self.ajax_post(f"/api/notifications/{notification_id}/read", {})
        member_response = self.ajax_post(
            f"/api/notifications/{notification_id}/read",
            {},
            client=member_client,
        )

        self.assertFalse(admin_response.get_json()["ok"])
        self.assertTrue(member_response.get_json()["ok"])

    def test_invalid_api_limits_are_bounded_instead_of_failing(self):
        self.create_user("admin@example.com")

        for path in (
            "/api/incidents?limit=invalid",
            "/api/notifications?limit=invalid",
            "/api/event-logs?limit=invalid",
            "/api/telemetry/vision?limit=invalid",
        ):
            response = self.client.get(path, headers={"Accept": "application/json"})
            self.assertEqual(response.status_code, 200, path)

    def test_production_startup_requires_session_secret(self):
        os.environ["APP_ENV"] = "production"
        os.environ["FLASK_SECRET"] = ""

        with self.assertRaisesRegex(RuntimeError, "FLASK_SECRET must be a strong value"):
            self.reload_app_module()

    def test_production_forces_secure_cookie_and_disables_voice_bypass(self):
        os.environ["APP_ENV"] = "production"
        os.environ["FLASK_SECRET"] = "production-test-secret-with-32-characters"
        os.environ["SESSION_COOKIE_SECURE"] = "0"
        os.environ.pop("ALLOW_LOCAL_VOICE_BYPASS", None)

        production_app = self.reload_app_module()
        production_backend = importlib.import_module("backend.app")

        self.assertTrue(production_app.app.config["SESSION_COOKIE_SECURE"])
        self.assertFalse(production_backend.ALLOW_LOCAL_VOICE_BYPASS)

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

    def test_profile_update_persists_extended_fields(self):
        response = self.ajax_post(
            "/register",
            {
                "full_name": "Akash Sharma",
                "email": "akash@example.com",
                "password": "password123",
            },
        )
        self.assertEqual(response.status_code, 200)

        update_response = self.ajax_post(
            "/api/profile",
            {
                "full_name": "Akash Sharma",
                "bio": "Night shift operator",
                "phone": "+91 9876543210",
                "location": "Main control room",
                "profile_visibility": "team",
                "activity_visibility": "admins",
                "alert_opt_in": False,
                "face_enrollment_opt_in": True,
                "privacy_policy_acknowledged": True,
            },
        )
        payload = update_response.get_json()["profile"]

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(payload["bio"], "Night shift operator")
        self.assertEqual(payload["phone"], "+91 9876543210")
        self.assertEqual(payload["location"], "Main control room")
        self.assertEqual(payload["profile_visibility"], "team")
        self.assertEqual(payload["activity_visibility"], "admins")
        self.assertFalse(payload["alert_opt_in"])
        self.assertTrue(payload["face_enrollment_opt_in"])
        self.assertTrue(payload["privacy_policy_acknowledged_at"])

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

    def test_camera_can_be_deleted_from_registry(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        delete_response = logged_in.delete(
            "/api/cameras/cam-2",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = delete_response.get_json()

        self.assertEqual(delete_response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["deleted"], "Gate Cam")
        self.assertEqual(payload["camera_count"], 1)

    def test_automation_api_updates_mode_and_environment(self):
        self.create_user()

        response = self.ajax_post(
            "/api/automation",
            {
                "mode": "manual",
                "environment": {
                    "temperature_c": 35,
                    "ambient_light": 20,
                    "humidity": 60,
                    "security_risk": "high",
                },
                "thresholds": {
                    "temperature_high_c": 31,
                    "temperature_low_c": 23,
                    "ambient_light_low": 25,
                    "ambient_light_high": 70,
                },
                "defense": {
                    "armed": True,
                    "auto_alarm": False,
                    "auto_defense": True,
                },
            },
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "manual")
        self.assertEqual(payload["environment"]["temperature_c"], 35.0)
        self.assertEqual(payload["thresholds"]["ambient_light_low"], 25.0)
        self.assertFalse(payload["defense"]["auto_alarm"])

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

    def test_camera_check_reports_local_camera_backend_status(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post(
            "/api/cameras/cam-1/check",
            {},
            client=logged_in,
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["check"]["camera_id"], "cam-1")
        self.assertEqual(payload["check"]["transport"], "local")
        self.assertFalse(payload["check"]["available"])
        self.assertEqual(payload["check"]["reason"], "camera_disabled")
        self.assertIn("status", payload["active_camera"])

    def test_camera_check_all_returns_every_registered_camera(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post("/api/cameras/check", {}, client=logged_in)
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["checks"]), 2)
        self.assertEqual({item["camera_id"] for item in payload["checks"]}, {"cam-1", "cam-2"})

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

    def test_camera_intelligence_api_persists_privacy_and_zones(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        update_response = self.ajax_post(
            "/api/camera-intelligence",
            {
                "privacy_mode": True,
                "sensitivity": 94,
                "detection": {"vehicle": True, "sound": True},
                "activity_zones": [
                    {"name": "Gate Line", "enabled": True, "x": -10, "y": 20, "w": 200, "h": 40},
                ],
                "patrol": {"enabled": True, "preset": "night_guard", "interval_seconds": 10},
                "quiet_hours": {"enabled": True, "start": "21:30", "end": "05:45"},
            },
            client=logged_in,
        )
        payload = update_response.get_json()["intelligence"]

        self.assertEqual(update_response.status_code, 200)
        self.assertTrue(payload["privacy_mode"])
        self.assertEqual(payload["sensitivity"], 94)
        self.assertTrue(payload["detection"]["vehicle"])
        self.assertTrue(payload["detection"]["sound"])
        self.assertEqual(payload["activity_zones"][0]["x"], 0)
        self.assertEqual(payload["activity_zones"][0]["w"], 100)
        self.assertEqual(payload["patrol"]["preset"], "night_guard")
        self.assertEqual(payload["patrol"]["interval_seconds"], 30)
        self.assertEqual(payload["quiet_hours"]["start"], "21:30")

        get_response = logged_in.get("/api/camera-intelligence", headers={"Accept": "application/json"})
        self.assertTrue(get_response.get_json()["intelligence"]["privacy_mode"])

    def test_camera_alert_exposes_privacy_mode_state(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)
        self.ajax_post("/api/camera-intelligence", {"privacy_mode": True}, client=logged_in)

        response = logged_in.get("/get_alert", headers={"Accept": "application/json"})
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["privacy_mode"])
        self.assertTrue(payload["intelligence"]["privacy_mode"])
        self.assertTrue(payload["camera_diagnostics"]["privacy_mode"])

    def test_discover_search_returns_incident_and_camera_records(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)
        self.ajax_post(
            "/api/incidents",
            {"title": "Unknown person at main entry", "severity": "warning", "camera_id": "cam-1"},
            client=logged_in,
        )

        response = self.ajax_post(
            "/api/discover",
            {"query": "unknown person main entry", "limit": 10},
            client=logged_in,
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(payload["total"], 1)
        self.assertTrue(any(item["type"] == "incident" for item in payload["items"]))

    def test_access_control_event_creates_critical_incident(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)

        response = self.ajax_post(
            "/api/access-control/events",
            {"event_type": "door_forced", "door_id": "main-entry", "actor": "Visitor"},
            client=logged_in,
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["event"]["severity"], "critical")

        incidents_response = logged_in.get("/api/incidents?severity=critical", headers={"Accept": "application/json"})
        titles = [item["title"] for item in incidents_response.get_json()["items"]]
        self.assertTrue(any("Access control alert" in title for title in titles))

    def test_journey_and_incident_report_endpoints_work(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)
        incident_response = self.ajax_post(
            "/api/incidents",
            {"title": "Vehicle near gate", "severity": "info", "camera_id": "cam-2"},
            client=logged_in,
        )
        incident_id = incident_response.get_json()["incident"]["id"]

        journey_response = logged_in.get("/api/journeys?target=person", headers={"Accept": "application/json"})
        report_response = logged_in.get(f"/api/incidents/{incident_id}/report", headers={"Accept": "application/json"})

        self.assertEqual(journey_response.status_code, 200)
        self.assertIn("path", journey_response.get_json())
        self.assertEqual(report_response.status_code, 200)
        self.assertEqual(report_response.get_json()["report"]["title"], f"Incident report #{incident_id}")

    def test_video_agent_status_describes_scene_and_behavior(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)
        self.backend_module.latest_detections = [{"label": "person", "count": 1}]
        self.backend_module.human_behavior["person_present"] = True
        self.backend_module.human_behavior["last_activity"] = "just_arrived"

        response = logged_in.get("/api/video-agent/status?lang=hinglish", headers={"Accept": "application/json"})
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertIn("speech_text", payload)
        self.assertEqual(payload["behavior_label"], "person_present")
        self.assertTrue(payload["should_speak"])

    def test_assistant_can_answer_video_scene_query_locally(self):
        self.create_user()

        logged_in = self.app_module.app.test_client()
        self.login_user(client=logged_in)
        self.backend_module.latest_detections = [{"label": "person", "count": 1}]

        response = self.ajax_post(
            "/assistant",
            {"query": "camera me kya dikh raha hai", "mode": "manual", "preferred_language": "hi-IN"},
            client=logged_in,
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["handled_locally"])
        self.assertEqual(payload["actions"][0]["type"], "video_agent")


if __name__ == "__main__":
    unittest.main()
