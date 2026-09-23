import styles from "./VolumeWarning.module.css";

/**
 * More than 50 sends a day from one mailbox is where providers start to notice (D10). A warning the
 * SDR reads before launching, never a block: the server keeps sending at the paced rate either way.
 */
export function VolumeWarning({ message }: { message: string }) {
  if (!message) return null;
  return (
    <p className={styles.volume} role="status">
      {message}
    </p>
  );
}
