import { useState } from "react";
import { useApiClient } from "@/app/AuthContext";
import { Button, Field, Input, Modal, useToast } from "@/components/ui";
import { ApiError } from "@/lib/api";
import type { Contact } from "@/lib/types";

/** "Other domain": search one person's email on a domain the rep knows they receive mail on
 *  (2026-10-29). For companies whose people use a parent group's or a regional brand's domain.
 *  Charged only when an address is kept, and the toast says which. */
export function FindEmailOnDomain({
  contact,
  onFound,
}: {
  contact: Contact;
  onFound: (contact: Contact) => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [domain, setDomain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function search() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.findEmailOnDomain(contact.id, domain.trim());
      onFound(res.contact);
      if (res.found) toast.success("Email found", res.message);
      else toast.toast({ tone: "info", title: "No email kept", description: res.message });
      setOpen(false);
      setDomain("");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        size="sm"
        variant="ghost"
        onClick={() => setOpen(true)}
        title="Search this person's email on another domain their company uses"
        aria-label={`Search ${contact.full_name}'s email on another domain`}
      >
        Other domain
      </Button>
      <Modal
        open={open}
        onClose={() => !busy && setOpen(false)}
        title={`Find ${contact.full_name}'s email on another domain`}
        description="For a company whose people receive mail somewhere other than its website. Charged one enrichment only if an address is found and verified."
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={search} loading={busy} disabled={!domain.trim()}>
              Search
            </Button>
          </>
        }
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (domain.trim() && !busy) void search();
          }}
        >
          <Field label="Domain" error={error ?? undefined} hint="For example acme-group.com">
            <Input
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              placeholder="acme-group.com"
              autoFocus
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
        </form>
      </Modal>
    </>
  );
}
