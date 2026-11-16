import importlib
import smtplib
import sys
import types
import unittest
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import SQLAlchemyError


class MailServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = SQLAlchemy()
        cls.app = Flask(__name__)
        cls.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True)
        cls.db.init_app(cls.app)
        cls.package_name = "_mail_service_test_app"
        package = types.ModuleType(cls.package_name)
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "app")]
        extensions = types.ModuleType(cls.package_name + ".extensions")
        extensions.db = cls.db
        modules = patch.dict(sys.modules, {cls.package_name: package, extensions.__name__: extensions})
        modules.start()
        cls.addClassCleanup(modules.stop)
        cls.models = importlib.import_module(cls.package_name + ".models")
        cls.settings = importlib.import_module(cls.package_name + ".settings")
        cls.mail = importlib.import_module(cls.package_name + ".mail_service")

    def setUp(self):
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(self.db.session.remove)
        self.db.metadata.drop_all(self.db.engine, tables=[self.models.AppSetting.__table__])
        self.db.metadata.create_all(self.db.engine, tables=[self.models.AppSetting.__table__])
        self.smtp = smtplib.SMTP(local_hostname="localhost")
        self.smtp.ehlo_or_helo_if_needed = Mock()
        self.smtp.sendmail = Mock(return_value={})
        self.smtp.quit = Mock(return_value=(221, b"Bye"))
        self.smtp.close = Mock()
        smtp_patch = patch.object(self.mail.smtplib, "SMTP", return_value=self.smtp)
        self.smtp_factory = smtp_patch.start()
        self.addCleanup(smtp_patch.stop)

    def message(self):
        message = EmailMessage()
        message["From"] = "MachMa Handwerker <handwerker@example.invalid>"
        message["To"] = "tenant@example.invalid"
        message["Subject"] = "Information zu Ihrem Auftrag"
        message.set_content("Der Handwerker kümmert sich um Ihren Auftrag.")
        return message

    def send(self, message):
        result = self.mail.send_email(message, smtp_host="smtp.example.invalid", smtp_port=2525)
        self.smtp_factory.assert_called_with("smtp.example.invalid", 2525, timeout=20)
        data = self.smtp.sendmail.call_args.args[2]
        return result, BytesParser(policy=policy.default).parsebytes(data)

    def assert_valid_headers(self, message):
        for name in ("Message-ID", "Date"):
            self.assertEqual(len(message.get_all(name)), 1)
            self.assertFalse(message[name].defects)
        self.assertRegex(str(message["Message-ID"]), r"^<[^\s<>@]+@[^\s<>@]+>$")
        self.assertIsNotNone(parsedate_to_datetime(str(message["Date"])).tzinfo)

    def test_missing_headers_are_added_to_smtp_data_without_changing_the_original(self):
        message = self.message()
        before = message.as_bytes()
        started = datetime.now(timezone.utc).replace(microsecond=0)
        result, sent = self.send(message)
        finished = datetime.now(timezone.utc)
        self.assertIsNone(result)
        self.assert_valid_headers(sent)
        self.assertTrue(str(sent["Message-ID"]).endswith("@example.invalid>"))
        self.assertTrue(started <= parsedate_to_datetime(str(sent["Date"])) <= finished)
        self.assertEqual(message.as_bytes(), before)
        self.assertEqual(str(sent["Subject"]), str(message["Subject"]))
        self.assertEqual(sent.get_content().splitlines(), message.get_content().splitlines())
        self.assertEqual(self.smtp.sendmail.call_args.args[:2], ("handwerker@example.invalid", ["tenant@example.invalid"]))

    def test_independent_messages_receive_different_message_ids(self):
        _, first = self.send(self.message())
        _, second = self.send(self.message())
        self.assertNotEqual(str(first["Message-ID"]), str(second["Message-ID"]))

    def test_existing_valid_ids_dates_and_reply_references_are_preserved(self):
        message = self.message()
        message["Message-ID"] = "<process-info-123-v2@example.invalid>"
        message["Date"] = "Mon, 16 Nov 2026 07:13:07 +0000"
        message["In-Reply-To"] = "<original@example.invalid>"
        message["References"] = "<original@example.invalid>"
        before = message.as_bytes()
        for _ in range(2):
            _, sent = self.send(message)
            self.assert_valid_headers(sent)
            for name in ("Message-ID", "Date", "In-Reply-To", "References"):
                self.assertEqual(str(sent[name]), str(message[name]))
        self.assertEqual(message.as_bytes(), before)

    def test_empty_and_malformed_message_ids_are_replaced(self):
        for value in ("", "not-an-id", "<bad id@example.invalid>", "<bad@example.invalid>>"):
            with self.subTest(value=value):
                message = self.message()
                message["Message-ID"] = value
                before = message.as_bytes()
                _, sent = self.send(message)
                self.assert_valid_headers(sent)
                self.assertTrue(str(sent["Message-ID"]).endswith("@example.invalid>"))
                self.assertEqual(message.as_bytes(), before)

    def test_empty_and_invalid_dates_are_replaced_without_changing_a_valid_message_id(self):
        for value in ("", "invalid date"):
            with self.subTest(value=value):
                message = self.message()
                message["Message-ID"] = "<stable@example.invalid>"
                message["Date"] = value
                _, sent = self.send(message)
                self.assert_valid_headers(sent)
                self.assertEqual(str(sent["Message-ID"]), "<stable@example.invalid>")

    def test_duplicate_message_ids_and_dates_are_replaced_by_single_valid_headers(self):
        data = self.message().as_bytes()
        message = BytesParser(policy=policy.default).parsebytes(
            b"Message-ID: <first@example.invalid>\nMessage-ID: <second@example.invalid>\n"
            b"Date: Mon, 16 Nov 2026 07:13:07 +0000\nDate: Mon, 16 Nov 2026 07:13:08 +0000\n" + data
        )
        before = message.as_bytes()
        _, sent = self.send(message)
        self.assert_valid_headers(sent)
        self.assertNotIn(str(sent["Message-ID"]), ("<first@example.invalid>", "<second@example.invalid>"))
        self.assertEqual(message.as_bytes(), before)

    def test_legacy_header_values_are_validated_before_sending(self):
        message = BytesParser().parsebytes(
            b"From: app@example.invalid\nTo: tenant@example.invalid\nMessage-ID: broken\nDate: invalid\n\nHello\n"
        )
        _, sent = self.send(message)
        self.assert_valid_headers(sent)

    def test_debug_redirection_retains_valid_headers_and_does_not_expose_original_recipients(self):
        self.db.session.add(self.models.AppSetting(
            key=self.settings.SettingKeys.SMTP_DEBUG_RECIPIENT.key,
            value_text="debug@example.invalid", value_type="string", label="Debug", group="SMTP",
        ))
        self.db.session.commit()
        message = self.message()
        message["Cc"] = "copy@example.invalid"
        message["Bcc"] = "hidden@example.invalid"
        message["Resent-From"] = "resent@example.invalid"
        message["Resent-To"] = "resent-recipient@example.invalid"
        message["Resent-Date"] = "Mon, 16 Nov 2026 07:13:07 +0000"
        message["Disposition-Notification-To"] = "receipt@example.invalid"
        message["Return-Receipt-To"] = "receipt@example.invalid"
        before = message.as_bytes()
        result, sent = self.send(message)
        self.assertEqual(result, "debug@example.invalid")
        self.assert_valid_headers(sent)
        self.assertEqual(str(sent["To"]), "debug@example.invalid")
        self.assertEqual(str(sent["Subject"]), "[DEBUG] Information zu Ihrem Auftrag")
        self.assertEqual(self.smtp.sendmail.call_args.args[1], ["debug@example.invalid"])
        self.assertFalse(any(name.lower() in {"cc", "bcc", "disposition-notification-to", "return-receipt-to"}
                             or name.lower().startswith("resent-") for name in sent.keys()))
        self.assertEqual(message.as_bytes(), before)

    def test_normal_recipients_and_hidden_envelope_recipients_are_preserved(self):
        message = self.message()
        message["Cc"] = "copy@example.invalid"
        message["Bcc"] = "hidden@example.invalid"
        _, sent = self.send(message)
        self.assert_valid_headers(sent)
        self.assertEqual(self.smtp.sendmail.call_args.args[1], ["tenant@example.invalid", "hidden@example.invalid", "copy@example.invalid"])
        self.assertEqual(str(sent["Cc"]), "copy@example.invalid")
        self.assertNotIn("Bcc", sent)
        self.assertIn("Bcc", message)

    def test_multipart_body_and_attachments_survive_header_completion(self):
        message = self.message()
        message.add_alternative("<p>Ihr Termin wurde bestätigt.</p>", subtype="html")
        calendar = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
        message.add_attachment(calendar, maintype="text", subtype="calendar", filename="termin.ics")
        _, sent = self.send(message)
        self.assert_valid_headers(sent)
        self.assertEqual(sent.get_body(preferencelist=("plain",)).get_content().splitlines(),
                         message.get_body(preferencelist=("plain",)).get_content().splitlines())
        self.assertIn("Ihr Termin wurde bestätigt", sent.get_body(preferencelist=("html",)).get_content())
        attachment = next(sent.iter_attachments())
        self.assertEqual(attachment.get_filename(), "termin.ics")
        self.assertEqual(attachment.get_payload(decode=True), calendar)

    def test_database_failure_blocks_smtp_without_changing_the_message(self):
        message = self.message()
        before = message.as_bytes()
        with patch.object(self.db.engine, "connect", side_effect=SQLAlchemyError("unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Debug-E-Mail-Einstellung"):
                self.mail.send_email(message, smtp_host="smtp.example.invalid", smtp_port=2525)
        self.smtp_factory.assert_not_called()
        self.assertEqual(message.as_bytes(), before)

    def test_invalid_debug_recipient_blocks_smtp(self):
        self.db.session.add(self.models.AppSetting(
            key=self.settings.SettingKeys.SMTP_DEBUG_RECIPIENT.key,
            value_text="not-an-address", value_type="string", label="Debug", group="SMTP",
        ))
        self.db.session.commit()
        with self.assertRaisesRegex(RuntimeError, "Debug-E-Mail-Einstellung"):
            self.mail.send_email(self.message(), smtp_host="smtp.example.invalid", smtp_port=2525)
        self.smtp_factory.assert_not_called()

    def test_transport_errors_are_reported_without_retrying(self):
        for error in (OSError("unavailable"), smtplib.SMTPDataError(550, b"rejected")):
            with self.subTest(error=type(error).__name__):
                self.smtp_factory.reset_mock()
                self.smtp_factory.side_effect = error
                with self.assertRaisesRegex(RuntimeError, "nicht versendet werden"):
                    self.mail.send_email(self.message(), smtp_host="smtp.example.invalid", smtp_port=2525)
                self.smtp_factory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
