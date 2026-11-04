import { useEffect, useId, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Field, Icons, Input, Modal, Skeleton, useToast } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { Account, SimilarPersonAdded } from "@/lib/types";
import styles from "./AddContactModal.module.css";

type AccountMode = "existing" | "new";

/** A person's own profile. The server is the authority (`person_profile_url`); this only saves a
 *  round trip for the commonest mistake, a company page pasted into the person's field. */
const PROFILE = /^(https?:\/\/)?([\w-]+\.)?linkedin\.com\/in\/[^/?#\s]+\/?(\?.*)?$/i;

export interface AddContactModalProps {
  open: boolean;
  onClose: () => void;
  onAdded: (result: SimilarPersonAdded) => void;
}

/**
 * Add one person by hand (SDR round 2, 5.4). The account is one the workspace has, found by
 * search, or a company named by its website: the server files the person under the account that
 * website already belongs to, and creates one only when there is none. Free; finding or verifying
 * their email is the separate, charged step it always was.
 */
export function AddContactModal({ open, onClose, onAdded }: AddContactModalProps) {
  const api = useApiClient();
  const toast = useToast();
  const navigate = useNavigate();
  const formId = useId();

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [title, setTitle] = useState("");
  const [phone, setPhone] = useState("");
  const [linkedin, setLinkedin] = useState("");
  const [mode, setMode] = useState<AccountMode>("existing");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Account[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [accountId, setAccountId] = useState("");
  const [website, setWebsite] = useState("");
  const [company, setCompany] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  // A fresh form every time it opens: a half-typed person from last time is somebody else.
  useEffect(() => {
    if (open) return;
    setName("");
    setEmail("");
    setTitle("");
    setPhone("");
    setLinkedin("");
    setMode("existing");
    setQuery("");
    setResults(null);
    setAccountId("");
    setWebsite("");
    setCompany("");
    setErrors({});
  }, [open]);

  // Search name and domain across the workspace, not only the newest 200 accounts.
  useEffect(() => {
    if (!open || mode !== "existing") return;
    const ctrl = new AbortController();
    const timer = setTimeout(() => {
      setSearching(true);
      api
        .listAccounts(ctrl.signal, query.trim() || undefined)
        .then((rows) => setResults(rows.slice(0, 8)))
        .catch(() => {
          if (!ctrl.signal.aborted) setResults([]);
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setSearching(false);
        });
    }, 250);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [api, open, mode, query]);

  function validate(): Record<string, string> {
    const out: Record<string, string> = {};
    if (!name.trim()) out.name = "Type their name.";
    const mail = email.trim();
    if (mail && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(mail)) out.email = "Like sam@acme.com.";
    if (linkedin.trim() && !PROFILE.test(linkedin.trim()))
      out.linkedin = "Their own profile, like linkedin.com/in/their-name. A company page can't be used.";
    if (mode === "existing" && !accountId) out.account = "Choose the account they work at.";
    if (mode === "new" && !/\.[a-z]{2,}/i.test(website.trim()))
      out.website = "The company's website, like acme.com.";
    return out;
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const found = validate();
    setErrors(found);
    if (Object.keys(found).length) return;
    setSaving(true);
    try {
      const res = await api.addContact({
        contact: {
          full_name: name.trim(),
          email: email.trim() || null,
          title: title.trim() || null,
          phone: phone.trim() || null,
          linkedin_url: linkedin.trim() || null,
        },
        ...(mode === "existing"
          ? { account_id: accountId }
          : { new_account_domain: website.trim(), new_account_name: company.trim() || undefined }),
      });
      const openAccount = {
        label: "Open account",
        onClick: () => navigate(`/accounts/${res.account_id}`),
      };
      if (res.created) {
        toast.toast({
          tone: "success",
          title: `Added ${res.contact.full_name}`,
          description: `Filed under ${res.account_name}${res.account_created ? ", a new account" : ""}.`,
          action: openAccount,
          durationMs: 8000,
        });
      } else {
        toast.toast({
          tone: "info",
          title: `${res.contact.full_name} is already in your workspace`,
          description: `Filed under ${res.account_name}. Nothing new was added.`,
          action: openAccount,
        });
      }
      onAdded(res);
      onClose();
    } catch (err) {
      toast.error("Couldn't add them", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Add a contact"
      description="One person, by hand. Adding them is free; finding their email is a separate step."
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button type="submit" form={formId} loading={saving}>
            Add contact
          </Button>
        </>
      }
    >
      <form id={formId} className={styles.form} onSubmit={submit} noValidate>
        <div className={styles.grid}>
          <Field label="Full name" required error={errors.name}>
            <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
          </Field>
          <Field label="Job title">
            <Input value={title} onChange={(e) => setTitle(e.target.value)} autoComplete="off" />
          </Field>
          <Field label="Work email" error={errors.email}>
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="sam@acme.com"
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <Field label="Phone">
            <Input
              type="tel"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="+1 415 555 0100"
              autoComplete="off"
            />
          </Field>
          <div className={styles.wide}>
            <Field
              label="LinkedIn profile"
              hint="Their own profile, linkedin.com/in/their-name."
              error={errors.linkedin}
            >
              <Input
                value={linkedin}
                onChange={(e) => setLinkedin(e.target.value)}
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
              />
            </Field>
          </div>
        </div>

        <fieldset className={styles.account}>
          <legend className={styles.legend}>Account</legend>
          <div className={styles.modes} role="radiogroup" aria-label="Which account">
            <label className={styles.mode} data-on={mode === "existing"}>
              <input
                type="radio"
                name={`${formId}-mode`}
                checked={mode === "existing"}
                onChange={() => setMode("existing")}
              />
              One you have
            </label>
            <label className={styles.mode} data-on={mode === "new"}>
              <input
                type="radio"
                name={`${formId}-mode`}
                checked={mode === "new"}
                onChange={() => setMode("new")}
              />
              A new company
            </label>
          </div>

          {mode === "existing" ? (
            <div className={styles.pick}>
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search accounts by name or website"
                iconLeft={<Icons.SearchIcon />}
                aria-label="Search accounts"
                autoComplete="off"
              />
              {results === null || (searching && results.length === 0) ? (
                <div className={styles.results}>
                  <Skeleton width="70%" height={14} />
                  <Skeleton width="55%" height={14} />
                </div>
              ) : results.length === 0 ? (
                <p className={styles.none}>
                  No account matches.{" "}
                  <button type="button" className={styles.link} onClick={() => setMode("new")}>
                    Add it as a new company
                  </button>
                </p>
              ) : (
                <div className={styles.results} role="radiogroup" aria-label="Accounts">
                  {results.map((a) => (
                    <label key={a.id} className={styles.result} data-on={accountId === a.id}>
                      <input
                        type="radio"
                        name={`${formId}-account`}
                        checked={accountId === a.id}
                        onChange={() => setAccountId(a.id)}
                      />
                      <span className={styles.resultName}>{a.name}</span>
                      {a.domain && <span className={styles.resultDomain}>{a.domain}</span>}
                    </label>
                  ))}
                </div>
              )}
              {errors.account && (
                <p className={styles.error} role="alert">
                  {errors.account}
                </p>
              )}
            </div>
          ) : (
            <div className={styles.grid}>
              <Field
                label="Website"
                required
                error={errors.website}
                hint="If an account already has this website, they go there."
              >
                <Input
                  value={website}
                  onChange={(e) => setWebsite(e.target.value)}
                  placeholder="acme.com"
                  inputMode="url"
                  autoComplete="off"
                  spellCheck={false}
                />
              </Field>
              <Field label="Company name" hint="Optional. The website is used when left blank.">
                <Input value={company} onChange={(e) => setCompany(e.target.value)} autoComplete="off" />
              </Field>
            </div>
          )}
        </fieldset>
      </form>
    </Modal>
  );
}
