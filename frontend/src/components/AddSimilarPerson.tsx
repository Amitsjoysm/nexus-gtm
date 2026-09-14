import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Field, Input, Select, useToast } from "@/components/ui";
import type { SelectOption } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { emailStatusMeta } from "@/lib/display";
import type { Account, Contact, ContactLookalike, SimilarPersonAdded } from "@/lib/types";
import styles from "./AddSimilarPerson.module.css";

const NEW_ACCOUNT = "__new__";

// Mirrors `normalise_name` in nexus/accounts/dedupe.py. Used only to SUGGEST which account a
// sourced person belongs under; the server decides identity (domain first, never a loose name).
const LEGAL_SUFFIX =
  /[\s,]+(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|co|co\.|gmbh|ag|sa|s\.a\.|bv|b\.v\.|plc|pty|pte|srl|oy|ab|as|nv|n\.v\.)\s*$/i;

export function normaliseCompanyName(name: string | null | undefined): string {
  let raw = (name ?? "").trim().toLowerCase();
  let previous: string | null = null;
  while (raw && previous !== raw) {
    previous = raw;
    raw = raw.replace(LEGAL_SUFFIX, "").replace(/^[\s,.]+|[\s,.]+$/g, "");
  }
  return raw.replace(/[^\p{L}\p{N}_\s]/gu, " ").replace(/\s+/g, " ").trim();
}

/** The result row after adding: now an ordinary workspace contact, so it gets Email and Call. */
export function lookalikeFromAdded(p: ContactLookalike, r: SimilarPersonAdded): ContactLookalike {
  return {
    ...p,
    is_new: false,
    contact_id: r.contact.id,
    account_id: r.account_id,
    account_name: r.account_name,
    email: r.contact.email,
    email_status: r.contact.email_status,
    linkedin_url: r.contact.linkedin_url ?? p.linkedin_url,
  };
}

/** A workspace result as the `Contact` the email composer and call console already take. */
export function contactFromLookalike(p: ContactLookalike): Contact {
  return {
    id: p.contact_id,
    account_id: p.account_id,
    full_name: p.full_name,
    title: p.title,
    seniority: p.seniority,
    email: p.email,
    phone: null,
    linkedin_url: p.linkedin_url,
    email_status: p.email_status ?? null,
    email_confidence: 0,
    email_checked_at: null,
    email_provider: null,
    phone_confidence: 0,
    enrichment_source: null,
  };
}

export interface AddSimilarPersonProps {
  person: ContactLookalike;
  onAdded: (result: SimilarPersonAdded) => void;
  onCancel: () => void;
  /** Lets a host that pads its rows bleed the form to their edges instead of nesting a panel. */
  className?: string;
}

/**
 * Keep a person "Source new people" found, inline under their result.
 *
 * Inline rather than a dialog: on the Contacts page the results already sit in one, and a dialog on
 * a dialog hides the list the rep is choosing from. The account is the rep's choice, preselected
 * when a workspace account has the same name as the employer on their profile; otherwise a new
 * account is created (or matched by domain on the server). Finding their work email is offered
 * here because a contact with no email cannot be written to, and it is the next thing a rep does.
 */
export function AddSimilarPerson({ person, onAdded, onCancel, className }: AddSimilarPersonProps) {
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const accounts = useApi<Account[]>((signal) => api.listAccounts(signal), []);

  const company = person.company.trim();
  const matches = useMemo(() => {
    const wanted = normaliseCompanyName(company);
    if (!wanted) return [];
    return (accounts.data ?? []).filter((a) => normaliseCompanyName(a.name) === wanted);
  }, [accounts.data, company]);

  // `null` follows the suggestion, so the matching account is selected once the list arrives
  // without ever overriding something the rep picked themselves.
  const [picked, setPicked] = useState<string | null>(null);
  const target = picked ?? matches[0]?.id ?? NEW_ACCOUNT;
  const creating = target === NEW_ACCOUNT;
  const chosen = creating ? null : (accounts.data ?? []).find((a) => a.id === target) ?? null;

  const [newName, setNewName] = useState(company);
  const [newDomain, setNewDomain] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [findEmail, setFindEmail] = useState(true);
  const [saving, setSaving] = useState(false);

  // An email is guessed from the company's domain, so without one there is nothing to look up.
  const hasDomain = creating ? newDomain.trim() !== "" : Boolean(chosen?.domain);

  const options = useMemo<SelectOption[]>(() => {
    const label = (a: Account) => (a.domain ? `${a.name} (${a.domain})` : a.name);
    const matched = new Set(matches.map((a) => a.id));
    const others = (accounts.data ?? [])
      .filter((a) => !matched.has(a.id))
      .sort((x, y) => x.name.localeCompare(y.name));
    return [
      { value: NEW_ACCOUNT, label: "Create a new account" },
      ...matches.map((a) => ({ value: a.id, label: label(a), group: "Matches their employer" })),
      ...others.map((a) => ({ value: a.id, label: label(a), group: "Other accounts" })),
    ];
  }, [accounts.data, matches]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const name = newName.trim();
    if (creating && !name) {
      setNameError("Name the company they work at.");
      return;
    }
    setSaving(true);
    try {
      const res = await api.addSimilarPerson({
        full_name: person.full_name,
        title: person.title,
        linkedin_url: person.linkedin_url,
        company,
        ...(creating
          ? { new_account_name: name, new_account_domain: newDomain.trim() || undefined }
          : { account_id: target }),
      });

      let contact = res.contact;
      let emailNote = "";
      // The server's answer decides, not the form: a new account may have been matched to an
      // existing one that does (or does not) carry a domain.
      if (findEmail && res.created && res.account_domain && !contact.email) {
        try {
          contact = await api.enrichContact(contact.id);
          emailNote = contact.email
            ? ` Work email ${contact.email} (${emailStatusMeta(contact.email_status).label}).`
            : " No work email found yet.";
        } catch (err) {
          emailNote = ` Their email wasn't looked up: ${
            err instanceof ApiError ? err.detail : "try Enrich on their row."
          }`;
        }
      }

      const openAccount = {
        label: "Open account",
        onClick: () => navigate(`/accounts/${res.account_id}`),
      };
      if (res.created) {
        toast.toast({
          tone: "success",
          title: `Added ${person.full_name}`,
          description: `Filed under ${res.account_name}${
            res.account_created ? ", a new account" : ""
          }.${emailNote}`,
          action: openAccount,
          durationMs: 8000,
        });
      } else {
        toast.toast({
          tone: "info",
          title: `${person.full_name} is already in your workspace`,
          description: `Filed under ${res.account_name}.`,
          action: openAccount,
        });
      }
      onAdded({ ...res, contact });
    } catch (err) {
      toast.error("Couldn't add them", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      className={cn(styles.form, className)}
      onSubmit={submit}
      aria-label={`Add ${person.full_name} to your workspace`}
    >
      <div className={styles.fields}>
        <Field
          label="Account"
          hint={
            accounts.loading
              ? "Loading your accounts…"
              : accounts.error
                ? "Couldn't load your accounts. A new account still reuses one with the same domain."
                : undefined
          }
        >
          <Select
            value={target}
            onChange={(e) => setPicked(e.target.value)}
            options={options}
            disabled={saving || accounts.loading}
          />
        </Field>
        {creating && (
          <>
            <Field label="Company name" required error={nameError ?? undefined}>
              <Input
                value={newName}
                onChange={(e) => {
                  setNewName(e.target.value);
                  setNameError(null);
                }}
                disabled={saving}
                autoComplete="off"
              />
            </Field>
            <Field
              label="Company domain"
              hint="Optional. Needed to find their email and to collect this company's signals."
            >
              <Input
                value={newDomain}
                onChange={(e) => setNewDomain(e.target.value)}
                placeholder="company.com"
                disabled={saving}
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
              />
            </Field>
          </>
        )}
      </div>

      <div className={styles.emailOption}>
        <label className={styles.check}>
          <input
            type="checkbox"
            checked={findEmail && hasDomain}
            disabled={!hasDomain || saving}
            onChange={(e) => setFindEmail(e.target.checked)}
          />
          <span>Find and verify their work email</span>
        </label>
        <p className={styles.checkHint}>
          {hasDomain
            ? "Uses enrichment credits, the same as Enrich on a contact."
            : "Needs the company's domain."}
        </p>
      </div>

      <div className={styles.actions}>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <Button type="submit" size="sm" loading={saving}>
          Add contact
        </Button>
      </div>
    </form>
  );
}
