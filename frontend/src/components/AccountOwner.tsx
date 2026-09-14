import { useId, useMemo, useState } from "react";
import { Button, Icons, Select, useToast } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { useCurrentUserId } from "@/app/useCurrentUserId";
import { ApiError } from "@/lib/api";
import type { Account, Member, Role } from "@/lib/types";
import styles from "./AccountOwner.module.css";

const ROLE_RANK: Record<Role, number> = { rep: 0, manager: 1, admin: 2, owner: 3 };

/** The select's value for "nobody". An empty string, because a `<select>` value is always a string. */
const UNOWNED = "";

type OwnerOption = { value: string; label: string; disabled?: boolean };

function memberLabel(m: Member, me: string | null): string {
  const name = m.full_name?.trim() || m.email;
  return m.user_id === me ? `${name} (you)` : name;
}

/**
 * Who works this account, and the one move each role has on it.
 *
 * Ownership decides whose "My accounts" alert routes an alert reaches, so it has to be visible where
 * the account is. A rep can claim an account nobody owns, or release their own. Taking one from a
 * colleague is a manager's call, which is why a rep looking at somebody else's account sees a name
 * and no control. The server enforces every rule here; this only avoids offering a move that 403s.
 */
export function AccountOwner({
  account,
  onChange,
}: {
  account: Account;
  onChange: (next: Account) => void;
}) {
  const api = useApiClient();
  const toast = useToast();
  const { session } = useAuth();
  const me = useCurrentUserId();
  const selectId = useId();
  const canAssign = session ? ROLE_RANK[session.role] >= ROLE_RANK.manager : false;
  const [busy, setBusy] = useState(false);

  // Only a manager reads the directory. It is the list of people an account can be given to, and
  // nothing a rep can do here needs it.
  const directory = useApi<Member[]>(
    (signal) => (canAssign ? api.memberDirectory(signal) : Promise.resolve([])),
    [canAssign],
  );

  const ownerId = account.owner_user_id ?? null;
  const mine = ownerId !== null && ownerId === me;

  const options = useMemo<OwnerOption[]>(() => {
    const people: OwnerOption[] = (directory.data ?? [])
      .map((m) => ({ value: m.user_id, label: memberLabel(m, me) }))
      .sort((a, b) => a.label.localeCompare(b.label));
    // An owner who has left the workspace is not in the directory. The select must still show who
    // holds the account today, rather than falling back to its first option and reading "Unowned".
    if (ownerId && !people.some((p) => p.value === ownerId)) {
      people.unshift({ value: ownerId, label: account.owner_name ?? "Former member", disabled: true });
    }
    return [{ value: UNOWNED, label: "Unowned" }, ...people];
  }, [directory.data, me, ownerId, account.owner_name]);

  async function assign(userId: string | null) {
    setBusy(true);
    try {
      const next = await api.setAccountOwner(account.id, userId);
      onChange(next);
      if (!next.owner_user_id) {
        toast.success(
          `${account.name} is unowned`,
          "Alerts on it no longer reach anyone's My accounts routes.",
        );
      } else if (next.owner_user_id === me) {
        toast.success(
          `You own ${account.name}`,
          "Your alert routes set to My accounts now include it.",
        );
      } else {
        toast.success(`${account.name} now belongs to ${next.owner_name ?? "a teammate"}`);
      }
    } catch (err) {
      toast.error(
        "Couldn't change the owner",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (canAssign) {
    return (
      <div className={styles.owner}>
        <label htmlFor={selectId} className={styles.label}>
          Owner
        </label>
        <Select
          id={selectId}
          className={styles.select}
          value={ownerId ?? UNOWNED}
          options={options}
          disabled={busy || directory.loading}
          onChange={(e) => assign(e.target.value || null)}
        />
      </div>
    );
  }

  return (
    <div className={styles.owner}>
      <span className={styles.icon} aria-hidden="true">
        <Icons.UserCheckIcon />
      </span>
      {ownerId === null ? (
        <span>Unowned</span>
      ) : (
        <span>
          Owned by{" "}
          <strong className={styles.name}>
            {mine ? "you" : (account.owner_name ?? "a former member")}
          </strong>
        </span>
      )}
      {ownerId === null && (
        <Button
          size="sm"
          variant="secondary"
          loading={busy}
          disabled={!me}
          onClick={() => assign(me)}
        >
          Claim account
        </Button>
      )}
      {mine && (
        <Button size="sm" variant="ghost" loading={busy} onClick={() => assign(null)}>
          Release
        </Button>
      )}
    </div>
  );
}

export default AccountOwner;
