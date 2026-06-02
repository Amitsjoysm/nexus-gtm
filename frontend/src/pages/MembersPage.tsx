import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Avatar,
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  Field,
  Icons,
  IconButton,
  Input,
  Modal,
  Select,
  useToast,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { humanize } from "@/lib/format";
import type { Member, Role } from "@/lib/types";
import styles from "./MembersPage.module.css";

const ROLE_RANK: Record<Role, number> = { owner: 3, admin: 2, manager: 1, rep: 0 };
const ROLE_OPTIONS: { value: Role; label: string }[] = [
  { value: "owner", label: "Owner" },
  { value: "admin", label: "Admin" },
  { value: "manager", label: "Manager" },
  { value: "rep", label: "Rep" },
];

const EMPTY_INVITE = {
  full_name: "",
  email: "",
  password: "",
  role: "rep" as Role,
};

export function MembersPage() {
  const api = useApiClient();
  const toast = useToast();
  const { session } = useAuth();
  const canManage = session ? ROLE_RANK[session.role] >= ROLE_RANK.admin : false;

  const members = useApi<Member[]>((signal) => api.listMembers(signal), []);
  const [modalOpen, setModalOpen] = useState(false);
  const [invite, setInvite] = useState(EMPTY_INVITE);
  const [submitting, setSubmitting] = useState(false);
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  async function changeRole(member: Member, role: Role) {
    setBusy((b) => ({ ...b, [member.membership_id]: true }));
    try {
      const updated = await api.changeMemberRole(member.membership_id, role);
      members.setData((prev) =>
        (prev ?? []).map((m) => (m.membership_id === member.membership_id ? updated : m)),
      );
      toast.success("Role updated", `${member.full_name} is now ${humanize(role)}.`);
    } catch (err) {
      toast.error(
        "Couldn't update role",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy((b) => {
        const next = { ...b };
        delete next[member.membership_id];
        return next;
      });
    }
  }

  async function remove(member: Member) {
    setBusy((b) => ({ ...b, [member.membership_id]: true }));
    try {
      await api.removeMember(member.membership_id);
      members.setData((prev) =>
        (prev ?? []).filter((m) => m.membership_id !== member.membership_id),
      );
      toast.success("Member removed", member.full_name);
    } catch (err) {
      toast.error(
        "Couldn't remove member",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setBusy((b) => {
        const next = { ...b };
        delete next[member.membership_id];
        return next;
      });
    }
  }

  async function onInvite(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    try {
      const created = await api.inviteMember({
        email: invite.email.trim(),
        full_name: invite.full_name.trim(),
        password: invite.password,
        role: invite.role,
      });
      members.setData((prev) => [...(prev ?? []), created]);
      toast.success("Member invited", created.full_name);
      setModalOpen(false);
      setInvite(EMPTY_INVITE);
    } catch (err) {
      toast.error(
        "Couldn't invite member",
        err instanceof ApiError ? err.detail : "Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const columns: Column<Member>[] = useMemo(
    () => [
      {
        key: "identity",
        header: "Member",
        render: (m) => (
          <div className={styles.identity}>
            <Avatar name={m.full_name} size="sm" />
            <div>
              <div className={styles.name}>{m.full_name}</div>
              <div className={styles.email}>{m.email}</div>
            </div>
          </div>
        ),
      },
      {
        key: "role",
        header: "Role",
        width: "180px",
        render: (m) =>
          canManage ? (
            <div className={styles.roleSelect}>
              <Select
                aria-label={`Role for ${m.full_name}`}
                value={m.role}
                disabled={busy[m.membership_id]}
                onChange={(e) => changeRole(m, e.target.value as Role)}
                options={ROLE_OPTIONS}
              />
            </div>
          ) : (
            <Badge tone="neutral">{humanize(m.role)}</Badge>
          ),
      },
      {
        key: "actions",
        header: "",
        align: "right",
        width: "60px",
        render: (m) =>
          canManage && m.role !== "owner" ? (
            <div className={styles.rowActions}>
              <IconButton
                label={`Remove ${m.full_name}`}
                icon={<Icons.XIcon />}
                onClick={() => remove(m)}
                disabled={busy[m.membership_id]}
              />
            </div>
          ) : null,
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [canManage, busy],
  );

  return (
    <div>
      <PageHeader
        title="Members"
        description="Manage who has access to your workspace and what they can do."
        actions={
          canManage ? (
            <Button iconLeft={<Icons.PlusIcon />} onClick={() => setModalOpen(true)}>
              Invite member
            </Button>
          ) : undefined
        }
      />

      <DataState
        state={members}
        skeleton={
          <Card padding="none">
            <DataTable<Member>
              columns={columns}
              rows={[]}
              getRowKey={(m) => m.membership_id}
              loading
            />
          </Card>
        }
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            icon={<Icons.UsersIcon />}
            title="No members yet"
            description="Invite your teammates to collaborate in this workspace."
            action={
              canManage ? (
                <Button iconLeft={<Icons.PlusIcon />} onClick={() => setModalOpen(true)}>
                  Invite member
                </Button>
              ) : undefined
            }
          />
        }
      >
        {(rows) => (
          <Card padding="none">
            <DataTable<Member>
              columns={columns}
              rows={rows}
              getRowKey={(m) => m.membership_id}
              caption="Workspace members"
            />
          </Card>
        )}
      </DataState>

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title="Invite member"
        description="They'll get access with the role you choose."
        footer={
          <>
            <Button variant="secondary" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button form="invite-form" type="submit" loading={submitting}>
              Send invite
            </Button>
          </>
        }
      >
        <form id="invite-form" className={styles.form} onSubmit={onInvite} noValidate>
          <Field label="Full name" required>
            <Input
              value={invite.full_name}
              onChange={(e) => setInvite({ ...invite, full_name: e.target.value })}
              placeholder="Jordan Rivera"
              autoComplete="name"
              required
            />
          </Field>
          <Field label="Work email" required>
            <Input
              type="email"
              value={invite.email}
              onChange={(e) => setInvite({ ...invite, email: e.target.value })}
              placeholder="jordan@company.com"
              autoComplete="email"
              required
            />
          </Field>
          <div className={styles.grid2}>
            <Field label="Temporary password" hint="At least 8 characters." required>
              <Input
                type="password"
                value={invite.password}
                onChange={(e) => setInvite({ ...invite, password: e.target.value })}
                placeholder="••••••••"
                autoComplete="new-password"
                minLength={8}
                required
              />
            </Field>
            <Field label="Role" required>
              <Select
                value={invite.role}
                onChange={(e) => setInvite({ ...invite, role: e.target.value as Role })}
                options={ROLE_OPTIONS}
              />
            </Field>
          </div>
        </form>
      </Modal>
    </div>
  );
}
