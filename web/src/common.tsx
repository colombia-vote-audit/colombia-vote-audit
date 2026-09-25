import { useEffect, useState, type DependencyList, type ReactNode } from "react";
import type { Position, VoteSummary } from "./api";
import { useI18n } from "./i18n";
import { previous } from "./router";

export function useFetch<T>(load: () => Promise<T>, deps: DependencyList) {
  const [state, setState] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: true });
  useEffect(() => {
    let live = true;
    setState((s) => ({ data: s.data, loading: true }));
    load().then(
      (data) => live && setState({ data, loading: false }),
      (e: Error) => live && setState({ error: e.message, loading: false }),
    );
    return () => {
      live = false;
    };
  }, deps);
  return state;
}

export function Status({ loading, error, children }: { loading: boolean; error?: string; children?: ReactNode }) {
  const { t } = useI18n();
  if (error) return <p className="status error">{t.error(error)}</p>;
  if (loading) return <p className="status">{t.loading}</p>;
  return <>{children}</>;
}

export function ResultBadge({ result }: { result: VoteSummary["result"] }) {
  const { t } = useI18n();
  return <span className={`badge result-${result}`}>{t.result[result]}</span>;
}

export function CheckBadge({ vote }: { vote: VoteSummary }) {
  const { t } = useI18n();
  if (vote.verified !== 0) return null;
  return (
    <span className="badge warn" title={t.checkFailed}>
      ⚠ {t.checkFailedBadge}
    </span>
  );
}

export function PositionBadge({ position }: { position: Position }) {
  const { t } = useI18n();
  return <span className={`badge pos-${position}`}>{t.voted[position]}</span>;
}

// The vote's date, marked when it isn't the vote's own session date.
export function VoteDate({ vote }: { vote: VoteSummary }) {
  const { t } = useI18n();
  const flagged = vote.date_source === "gazette" || vote.date_source === "publication";
  return (
    <span className={`date${flagged ? " flagged" : ""}`} title={flagged ? t.dateFlag[vote.date_source as "gazette"] : undefined}>
      {vote.date ?? "—"}
      {flagged && <sup>*</sup>}
    </span>
  );
}

export function subtitle(vote: VoteSummary, t: ReturnType<typeof useI18n>["t"]): string {
  return [vote.bill_name, vote.chamber, vote.vote_type && t.voteType[vote.vote_type]].filter(Boolean).join(" · ");
}

export function Avatar({ name, photo, size }: { name: string; photo: string | null; size: number }) {
  const [broken, setBroken] = useState(false);
  const initials = name
    .split(/\s+/)
    .filter((w) => /^\p{Lu}/u.test(w))
    .slice(0, 2)
    .map((w) => w[0])
    .join("");
  return photo && !broken ? (
    <img className="avatar" src={photo} alt="" width={size} height={size} onError={() => setBroken(true)} />
  ) : (
    <span className="avatar initials" style={{ width: size, height: size, fontSize: size * 0.34 }}>
      {initials}
    </span>
  );
}

export function termYears(start: string, end: string): string {
  return `${start.slice(0, 4)}–${end.slice(0, 4)}`;
}

// 'Cámara de Representantes' -> 'Cámara', to match the gazettes' labels.
export function shortChamber(chamber: string): string {
  return chamber.split(" ")[0];
}

export interface Back {
  href: string;
  label: string;
}

// Goes back in the browser's history when the previous page is the target,
// so the back button afterwards still behaves.
export function BackLink({ back }: { back: Back }) {
  return (
    <a
      className="back"
      href={back.href}
      onClick={(e) => {
        if (previous() === back.href) {
          e.preventDefault();
          history.back();
        }
      }}
    >
      ← {back.label}
    </a>
  );
}

export function useTitle(title: string | null | undefined) {
  const { t } = useI18n();
  useEffect(() => {
    if (title) document.title = `${title} · ${t.title}`;
  }, [title, t]);
}
