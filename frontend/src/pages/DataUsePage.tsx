import { Link } from "react-router-dom";
import styles from "./DataUsePage.module.css";

/**
 * What "Help improve the AI with this workspace's data" collects (D24, spec §18).
 *
 * Public, because the sign-up form links here before an account exists. Plain words, no marketing:
 * the choice is only informed if this page says exactly what is kept, where, and how to stop it.
 * Keep it in step with `nexus/engagement/ledger/envelope.py::EVENT_TYPES` and spec §18.
 */
export function DataUsePage() {
  return (
    <main className={styles.page}>
      <article className={styles.article}>
        <h1>How workspace data improves the AI</h1>
        <p className={styles.lede}>
          When this is on, the product records what happens in your workspace so that its drafting,
          reply reading and timing advice can be improved, and so that SDRs can see when a prospect
          usually replies. You can switch it off at any time under Settings; switching off stops
          collection immediately and deletes what was collected.
        </p>

        <h2>What is recorded</h2>
        <ul>
          <li>AI requests and results: drafted emails, research briefs, reply readings, the model used.</li>
          <li>How your team used them: edits to drafts, approvals, decisions on replies.</li>
          <li>Outreach outcomes: emails sent, bounces, replies, meetings, and when they happened.</li>
          <li>Account activity the product already stores: signals, scores, enrichment, calls.</li>
        </ul>
        <p>Clicks and page views in the browser are not recorded.</p>

        <h2>Two uses, kept apart</h2>
        <ul>
          <li>
            <strong>Training.</strong> Before anything is used to train a model, names, email
            addresses, phone numbers, company names and links are replaced with placeholders, and
            people, companies and workspaces are replaced with one-way keys.
          </li>
          <li>
            <strong>Prospect insights.</strong> Facts such as when a person tends to reply are kept
            with the person so SDRs can be told. Another workspace sees a pattern only when at least
            three workspaces have history with that person, and never who emailed them, what was
            said, or which workspaces.
          </li>
        </ul>

        <h2>Who can change it</h2>
        <p>
          Workspace owners and admins. A person can ask for their data to be erased; erasure removes
          it from every store.
        </p>

        <p className={styles.back}>
          <Link to="/login">Back to sign up</Link>
        </p>
      </article>
    </main>
  );
}

export default DataUsePage;
