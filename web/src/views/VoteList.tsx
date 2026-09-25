import { useEffect, useState } from "react";
import { api, type VoteSummary } from "../api";
import { CheckBadge, ResultBadge, Status, VoteDate, subtitle } from "../common";
import { useI18n } from "../i18n";
import { href } from "../router";

const PAGE = 50;

export function VoteList({ q, from, to }: { q: string; from: string; to: string }) {
  const { t, num } = useI18n();
  const [votes, setVotes] = useState<VoteSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [state, setState] = useState<{ loading: boolean; error?: string }>({ loading: true });

  const load = (offset: number, live: () => boolean = () => true) => {
    setState({ loading: true });
    api.votes({ q, from, to, offset, limit: PAGE }).then(
      (r) => {
        if (!live()) return;
        setVotes((vs) => (offset ? [...vs, ...r.votes] : r.votes));
        setTotal(r.total);
        setState({ loading: false });
      },
      (e: Error) => live() && setState({ loading: false, error: e.message }),
    );
  };

  useEffect(() => {
    let live = true;
    load(0, () => live);
    return () => {
      live = false;
    };
  }, [q, from, to]);

  return (
    <section>
      <p className="count">{state.loading && !votes.length ? t.loading : t.votesFound(num(total))}</p>
      <ul className="rows">
        {votes.map((v) => (
          <li key={v.id}>
            <a className="row" href={href(`/vote/${v.id}`)}>
              <VoteDate vote={v} />
              <span className="main">
                <strong>{v.subject ?? v.bill_name ?? `#${v.id}`}</strong>
                <small>{subtitle(v, t)}</small>
              </span>
              <span className="side">
                <span className="badges">
                  <CheckBadge vote={v} />
                  <ResultBadge result={v.result} />
                </span>
                <small className="mono">{t.countsLine(v.counts)}</small>
              </span>
            </a>
          </li>
        ))}
      </ul>
      {!state.loading && !state.error && !votes.length && <p className="status">{t.noVotes}</p>}
      <Status loading={state.loading && !!votes.length} error={state.error}>
        {votes.length < total && (
          <button className="more" onClick={() => load(votes.length)}>
            {t.loadMore}
          </button>
        )}
      </Status>
    </section>
  );
}
