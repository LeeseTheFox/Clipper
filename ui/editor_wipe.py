"""Shared confirmation UI for permanently removing a clip's editor data."""

from __future__ import annotations

from i18n import _


def present_wipe_edits_confirmation(adw, parent, clip_name, callback, *callback_args):
    """Present the destructive confirmation shared by Clips and the editor."""
    dialog = adw.MessageDialog.new(parent)
    dialog.set_heading(_("Wipe all current edits?"))
    dialog.set_body(
        _("All cuts and audio changes for '%(name)s' will be permanently removed.")
        % {"name": clip_name}
    )
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("wipe", _("Wipe edits"))
    dialog.set_response_appearance("wipe", adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect("response", callback, *callback_args)
    dialog.present()
    return dialog
