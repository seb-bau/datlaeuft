from html import unescape
import re
import unittest

import test_external_portal_light as fixtures


class ExternalPortalTextTests(unittest.TestCase):
    setUp = fixtures.ExternalPortalLightTests.setUp
    start_patch = fixtures.ExternalPortalLightTests.start_patch
    submit = fixtures.ExternalPortalLightTests.submit
    schedule = fixtures.ExternalPortalLightTests.schedule

    @classmethod
    def setUpClass(cls):
        fixtures.ExternalPortalLightTests.setUpClass.__func__(cls)

    def values(self, **changes):
        return {**self.portal_texts.portal_text_definition().default, **changes}

    def save_texts(self, **changes):
        values = self.values(**changes)
        self.settings.set(self.portal_texts.portal_text_definition(), self.portal_texts.validate_portal_texts(values))
        self.db.session.commit()
        return values

    def editor(self, *, action="preview", state="accept", **changes):
        return self.client.post("/admin/settings/email-templates", data={
            "template_key": "external_portal_texts", "action": action, "preview_state": state,
            **{"text_" + key: value for key, value in self.values(**changes).items()},
        })

    def frame(self, response):
        match = re.search(r'srcdoc="([^"]*)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return unescape(match.group(1))

    def test_editor_and_mail_selector_expose_all_templates(self):
        page = self.client.get("/admin/settings/email-templates?template=external_portal_texts")
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="text_intro_accept"', page.get_data(as_text=True))
        self.assertIn("Bestätigung und Portalzugang", page.get_data(as_text=True))
        page = self.client.get("/admin/settings/email-templates?template=external_reminder_second")
        self.assertEqual(page.status_code, 200)
        for text in ("Erinnerungsmail 1", "Erinnerungsmail 2", "Stornierung ohne Rückmeldung", "Antwortseite und Mail-Schaltflächen"):
            self.assertIn(text, page.get_data(as_text=True))

    def test_saved_copy_is_used_on_live_choice_and_confirmation_pages(self):
        response = self.editor(action="save", intro_accept="Guten Tag {{ craftsman_name }}! Bitte Auftrag {{ commission_number }} bestätigen.",
                               intro_reject="Bitte Ihre Absage zu {{ commission_number }} bestätigen.",
                               submit_label="Rückmeldung verbindlich senden", accepted_message="Danke für Ihre Zusage zu {{ commission_number }}.",
                               portal_heading="Ihr Zugang bei {{ portal_name }}", portal_description="Alle Aufträge bequem online bearbeiten.")
        self.assertEqual(response.status_code, 302)
        page = self.client.get(self.path).get_data(as_text=True)
        self.assertIn("Guten Tag Externe Firma! Bitte Auftrag A-100 bestätigen.", page)
        self.assertIn("Rückmeldung verbindlich senden", page)
        self.assertIn('data-reject-intro="Bitte Ihre Absage zu A-100 bestätigen."', page)
        self.assertIsNone(self.mail.response_at)
        self.assertEqual(self.submit().status_code, 303)
        page = self.client.get(self.path).get_data(as_text=True)
        self.assertIn("Danke für Ihre Zusage zu A-100.", page)
        self.assertIn("Alle Aufträge bequem online bearbeiten.", page)

    def test_saved_rejection_and_unavailable_text_are_used_without_changing_their_actions(self):
        self.save_texts(rejected_message="Ihre Absage zu {{ commission_number }} wurde gespeichert.",
                        unavailable_heading="Link abgelaufen", unavailable_message="Bitte melden Sie sich beim Auftragsservice.")
        self.assertEqual(self.submit("reject").status_code, 303)
        self.assertIn("Ihre Absage zu A-100 wurde gespeichert.", self.client.get(self.path).get_data(as_text=True))
        page = self.client.get(self.path + "tampered")
        self.assertEqual(page.status_code, 410)
        self.assertIn("Bitte melden Sie sich beim Auftragsservice.", page.get_data(as_text=True))

    def test_previews_render_every_step_from_unsaved_values_without_saving_or_processing_orders(self):
        self.save_texts(intro_accept="Gespeicherte Einleitung")
        row = self.models.AppSetting.query.filter_by(key=self.portal_texts.portal_text_definition().key).one()
        saved = row.value_text
        for state in self.portal_texts.PORTAL_PREVIEW_STATES:
            with self.subTest(state=state):
                response = self.editor(state=state, intro_accept="Entwurf für {{ commission_number }}", intro_reject="Absageentwurf",
                                       accepted_message="Zusageentwurf", rejected_message="Bestätigte Absage", portal_description="Portalentwurf")
                self.assertEqual(response.status_code, 200)
                frame = self.frame(response)
                if state == "accept":
                    self.assertIn("Entwurf für 2026-0042", frame)
                if state in {"accepted", "rejected", "existing"}:
                    self.assertIn("Portalentwurf", frame)
                self.assertIn('sandbox referrerpolicy="no-referrer"', response.get_data(as_text=True))
        self.assertEqual(row.value_text, saved)
        self.assertIn("Gespeicherte Einleitung", self.client.get(self.path).get_data(as_text=True))
        self.assertEqual(self.models.ErpOutboxOperation.query.count(), 0)
        self.assertIsNone(self.mail.response_at)
        self.assertFalse(self.craftsman.portal_enabled)

    def test_reset_restores_default_texts(self):
        self.save_texts(intro_accept="Eigene Einleitung")
        self.assertEqual(self.editor(action="reset").status_code, 302)
        self.assertEqual(self.portal_texts.get_portal_text_templates(), self.portal_texts.portal_text_definition().default)
        self.assertEqual(self.models.AuditLog.query.filter_by(action="external_portal_texts.reset").count(), 1)

    def test_invalid_templates_cannot_replace_saved_values(self):
        self.save_texts(intro_accept="Bisheriger Text")
        for text in ("{{ unknown_field }}", "{{ commission_number.__class__ }}", "{{ range(10) }}", "{% for value in values %}x{% endfor %}", "{{", "x" * 4001, "Text\x00"):
            with self.subTest(text=text[:50]):
                self.assertEqual(self.editor(action="save", intro_accept=text).status_code, 200)
                self.assertEqual(self.portal_texts.get_portal_text_templates()["intro_accept"], "Bisheriger Text")
        self.assertEqual(self.editor(action="save", accept_label="Annehmen\nAblehnen").status_code, 200)
        self.assertEqual(self.models.AuditLog.query.count(), 0)

    def test_text_and_placeholder_content_are_escaped_on_the_page_and_in_buttons(self):
        self.save_texts(intro_accept="Hinweis <script>alert(1)</script> {{ commission_title }}", accept_label="Zusage <bestätigen>")
        self.commission.title = "<b>Rohrbruch</b>"
        self.db.session.commit()
        page = self.client.get(self.path).get_data(as_text=True)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("&lt;b&gt;Rohrbruch&lt;/b&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.external.send_external_assignment_email(self.mail)
        message = self.external_send.call_args.args[0]
        self.assertIn("Zusage &lt;bestätigen&gt;</a>", message.get_body(preferencelist=("html",)).get_content())

    def test_assignment_subject_body_and_action_texts_are_customizable(self):
        self.save_texts(mail_actions_intro="Bitte antworten Sie zu {{ commission_number }}:", accept_label="Ich übernehme", reject_label="Ich kann nicht")
        self.settings.set(self.mail_templates.email_template_definition("external_assignment"), {
            "subject": "Beauftragung {{ commission_number }}", "body": "Sehr geehrte Firma {{ recipient_name }},\nbitte bearbeiten Sie {{ commission_title }}.",
        })
        self.db.session.commit()
        self.external.send_external_assignment_email(self.mail)
        message = self.external_send.call_args.args[0]
        self.assertEqual(str(message["Subject"]), "Beauftragung A-100")
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Sehr geehrte Firma Externe Firma", plain)
        self.assertIn("Bitte antworten Sie zu A-100:", plain)
        self.assertIn("Ich übernehme: ", plain)
        self.assertIn("decision=accept", plain)
        self.assertIn("Ich kann nicht</a>", message.get_body(preferencelist=("html",)).get_content())

    def test_reminders_and_cancellation_use_their_independent_templates(self):
        for key, subject in (("external_reminder", "Erste Nachfrage"), ("external_reminder_second", "Letzte Nachfrage"), ("external_cancellation", "Stornierung")):
            self.settings.set(self.mail_templates.email_template_definition(key), {"subject": subject + " {{ commission_number }}", "body": "Eigener Inhalt {{ days }} Tage"})
        self.db.session.commit()
        for days, expected in ((2, "Erste Nachfrage A-100"), (4, "Letzte Nachfrage A-100"), (6, "Stornierung A-100")):
            self.schedule(days)
            self.notifications.process_workflow_notification_batch()
            message = self.send.call_args.args[0]
            self.assertEqual(str(message["Subject"]), expected)
            self.assertIn(f"Eigener Inhalt {days} Tage", message.get_body(preferencelist=("plain",)).get_content())

    def test_existing_shared_reminder_template_is_preserved_until_second_template_is_saved(self):
        first = {"subject": "Vorhandene Vorlage {{ reminder_number }}", "body": "Bestehender Text"}
        self.settings.set(self.mail_templates.email_template_definition("external_reminder"), first)
        self.db.session.commit()
        self.assertEqual(self.mail_templates.get_email_template("external_reminder_second"), first)
        second = {"subject": "Eigene zweite Vorlage", "body": "Anderer Text"}
        self.settings.set(self.mail_templates.email_template_definition("external_reminder_second"), second)
        self.settings.set(self.mail_templates.email_template_definition("external_reminder"), {"subject": "Neu", "body": "Neue erste Vorlage"})
        self.db.session.commit()
        self.assertEqual(self.mail_templates.get_email_template("external_reminder_second"), second)

    def test_email_preview_includes_custom_buttons_and_the_correct_reminder_number(self):
        self.save_texts(accept_label="Eigene Zusage", reject_label="Eigene Absage")
        response = self.client.post("/admin/settings/email-templates", data={
            "template_key": "external_reminder_second", "action": "preview",
            "subject": "Nachfrage {{ reminder_number }}", "body": "Entwurf {{ commission_number }}",
        })
        self.assertEqual(response.status_code, 200)
        frame = self.frame(response)
        self.assertIn("Eigene Zusage</a>", frame)
        self.assertIn("Nachfrage 2", frame)
        self.assertIn("Entwurf 2026-0042", frame)
        self.assertIsNone(self.models.AppSetting.query.filter_by(key="email.templates.external_reminder_second").one_or_none())
        self.send.assert_not_called()
        self.external_send.assert_not_called()

    def test_shared_layout_preview_keeps_the_proposed_layout_and_external_buttons(self):
        response = self.client.post("/admin/settings/email-templates", data={
            "template_key": "layout", "action": "preview", "preview_template": "external_reminder_second",
            "html": "<!doctype html><html><body><h1>Eigener Briefkopf</h1>{{ content }}</body></html>",
        })
        self.assertEqual(response.status_code, 200)
        frame = self.frame(response)
        self.assertIn("Eigener Briefkopf", frame)
        self.assertIn("Auftrag annehmen</a>", frame)
        self.assertIn("Erinnerung 2", response.get_data(as_text=True))
        self.assertIsNone(self.models.AppSetting.query.filter_by(key="email.layout").one_or_none())


if __name__ == "__main__":
    unittest.main()
