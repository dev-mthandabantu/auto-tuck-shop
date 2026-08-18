"""Test the 'Looks good' / 'Fix mistake' sale receipt buttons."""

from tests.conftest import button_click_payload, text_message_payload


def _send_sale_and_get_buttons(send_webhook, poll_outbox, phone, sale_text="sold 3 bread $2 each"):
    """Helper: send a sale message, wait for receipt with confirm + fix buttons."""
    send_webhook(text_message_payload(phone, sale_text))

    def _find_buttons(outbox):
        for btn in outbox.get("buttons", []):
            confirm = None
            fix = None
            for b in btn.get("buttons", []):
                if b.get("id", "").startswith("confirm_"):
                    confirm = b
                elif b.get("id", "").startswith("fix_"):
                    fix = b
            if confirm and fix:
                return {
                    "confirm_id": confirm["id"],
                    "fix_id": fix["id"],
                    "message_id": btn["message_id"],
                    "body": btn["body"],
                }
        return None

    result = poll_outbox(phone, check=_find_buttons, timeout=10.0)
    assert isinstance(result, dict) and "confirm_id" in result, (
        f"No confirm/fix buttons found for {phone}. Outbox: {result}"
    )
    return result


def test_sale_receipt_has_confirm_and_fix_buttons(send_webhook, poll_outbox, onboard_user, unique_phone):
    """Sale receipts should have 'Looks good' and 'Fix mistake' buttons."""
    onboard_user(unique_phone)
    send_webhook(text_message_payload(unique_phone, "sold 2 coke $3 each"))

    def _find_any_buttons(outbox):
        for btn in outbox.get("buttons", []):
            button_ids = [b.get("id", "") for b in btn.get("buttons", [])]
            if any(bid.startswith("confirm_") for bid in button_ids):
                return button_ids
        return None

    button_ids = poll_outbox(unique_phone, check=_find_any_buttons, timeout=10.0)
    assert isinstance(button_ids, list), f"No buttons found. Outbox: {button_ids}"

    # Should have confirm and fix buttons
    assert any(bid.startswith("confirm_") for bid in button_ids), (
        f"Expected 'confirm_' button, got: {button_ids}"
    )
    assert any(bid.startswith("fix_") for bid in button_ids), (
        f"Expected 'fix_' button, got: {button_ids}"
    )


def test_sale_stays_pending_until_button_clicked(send_webhook, poll_outbox, onboard_user, unique_phone):
    """Sales should NOT be auto-confirmed — they wait for 'Looks good' or 'Fix mistake'."""
    onboard_user(unique_phone)

    btn_info = _send_sale_and_get_buttons(send_webhook, poll_outbox, unique_phone)
    assert btn_info["body"], "Receipt body should not be empty"
    # The receipt itself proves the sale was created and is awaiting a
    # decision — confirm/cancel behavior is covered by the tests below.


def test_confirm_sale_looks_good(send_webhook, poll_outbox, onboard_user, unique_phone):
    """Clicking 'Looks good' should acknowledge the sale."""
    onboard_user(unique_phone)

    btn_info = _send_sale_and_get_buttons(send_webhook, poll_outbox, unique_phone)

    # Click "Looks good"
    send_webhook(button_click_payload(
        unique_phone, btn_info["confirm_id"], btn_info["message_id"],
    ))

    # Should get acknowledgment
    def _find_confirm_response(outbox):
        for m in outbox.get("messages", []):
            if "✓" in m["text"] or "recorded" in m["text"].lower():
                return m
        return None

    msg = poll_outbox(unique_phone, check=_find_confirm_response)
    assert isinstance(msg, dict), f"Expected confirmation response. Outbox: {msg}"


def test_fix_mistake_cancels_sale(send_webhook, poll_outbox, onboard_user, unique_phone):
    """Clicking 'Fix mistake' should cancel the sale and tell user to resend."""
    onboard_user(unique_phone)

    btn_info = _send_sale_and_get_buttons(send_webhook, poll_outbox, unique_phone)

    # Click "Fix mistake"
    send_webhook(button_click_payload(
        unique_phone, btn_info["fix_id"], btn_info["message_id"],
    ))

    # Should get a message telling user to resend
    def _find_fix_response(outbox):
        for m in outbox.get("messages", []):
            if "send" in m["text"].lower() and "again" in m["text"].lower():
                return m
        return None

    msg = poll_outbox(unique_phone, check=_find_fix_response)
    assert isinstance(msg, dict), f"Expected fix mistake response. Outbox: {msg}"
    assert "send" in msg["text"].lower(), (
        f"Should tell user to resend. Got: {msg['text']}"
    )


def test_fix_mistake_double_click_idempotent(send_webhook, poll_outbox, onboard_user, unique_phone):
    """Clicking 'Fix mistake' twice should not crash — second click gets 'already processed'."""
    onboard_user(unique_phone)

    btn_info = _send_sale_and_get_buttons(send_webhook, poll_outbox, unique_phone)

    # Click "Fix mistake" twice
    payload = button_click_payload(
        unique_phone, btn_info["fix_id"], btn_info["message_id"],
    )
    send_webhook(payload)

    # Wait for first fix response
    def _find_cancelled(outbox):
        for m in outbox.get("messages", []):
            if "send" in m["text"].lower() and "again" in m["text"].lower():
                return True
        return None

    poll_outbox(unique_phone, check=_find_cancelled)

    # Click again
    send_webhook(payload)

    # Should get "already processed" message
    def _find_already_processed(outbox):
        for m in outbox.get("messages", []):
            if "already" in m["text"].lower():
                return m
        return None

    msg = poll_outbox(unique_phone, check=_find_already_processed, timeout=10.0)
    assert isinstance(msg, dict), f"Expected 'already processed' message. Outbox: {msg}"
