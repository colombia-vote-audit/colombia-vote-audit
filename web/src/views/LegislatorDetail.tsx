import { useState } from "react";
import { api, type LegislatorDetail as Legislator, type Position } from "../api";
import { Avatar, PositionBadge, Status, VoteDate, shortChamber, termYears, useFetch } from "../common";
import { useI18n } from "../i18n";
import { href } from "../router";

const PAGE = 100;

export function LegislatorDetail({ id, back }: { id: number; back: string }) {
  const { t } = useI18n();
  const { data, loading, error } = useFetch(() => api.legislator(id), [id]);
  return (
    <section className="detail">
      <a className="back" href={back}>
        ← {t.backToSearch}
      </a>
      <Status loading={loading && !data} error={error}>
        {data && <Body p={data} key={data.id} />}
      </Status>
    </section>
  );
}

function Body({ p }: { p: Legislator }) {
  const { t, num } = useI18n();
  const [shown, setShown] = useState(PAGE);
  const cast = p.totals.yes + p.totals.no + p.totals.abstain;
  const span = p.terms.length ? termYears(p.terms[0].start, p.terms[p.terms.length - 1].end) : null;
  const stats: [string, number, Position | "cast"][] = [
    [t.votesCast, cast, "cast"],
    [t.position.yes, p.totals.yes, "yes"],
    [t.position.no, p.totals.no, "no"],
    [t.position.abstain, p.totals.abstain, "abstain"],
    [t.position.absent, p.totals.absent, "absent"],
  ];
  return (
    <>
      <header className="profile">
        <Avatar name={p.name} photo={p.photo_url} size={88} />
        <div>
          <h1>{p.name}</h1>
          <p className="line">
            {p.party && <span>{p.party}</span>}
            {span && <span className="mono">{span}</span>}
          </p>
          {p.service.map((s) => (
            <p className="line" key={`${s.chamber}${s.term_start}`}>
              <span>{shortChamber(s.chamber)}</span>
              <span className="mono">{t.inOffice(s.first_vote, s.last_vote)}</span>
            </p>
          ))}
          {p.terms.map((term) => (
            <p className="line muted" key={`${term.chamber}${term.start}`}>
              <span>{shortChamber(term.chamber)}</span>
              <span className="mono">{termYears(term.start, term.end)}</span>
              {term.party && <span>{term.party}</span>}
            </p>
          ))}
        </div>
      </header>

      <div className="stats">
        {stats.map(([label, n, kind]) => (
          <div key={kind} className={`c-${kind}`}>
            <b>{num(n)}</b>
            <span>{label}</span>
          </div>
        ))}
      </div>
      <p className="note">{t.totalsNote}</p>

      <h2 className="section">{t.votingRecord}</h2>
      <ul className="rows">
        {p.record.slice(0, shown).map((r) => (
          <li key={`${r.id}-${r.position}`}>
            <a className={`row${r.position === "absent" ? " dim" : ""}`} href={href(`/vote/${r.id}`)}>
              <VoteDate vote={r} />
              <span className="main">
                <strong>{r.subject ?? r.bill_name ?? `#${r.id}`}</strong>
                <small>
                  {[r.chamber, r.party_then && t.partyThen(r.party_then), t.voteResult(t.result[r.result])]
                    .filter(Boolean)
                    .join(" · ")}
                </small>
              </span>
              <span className="side">
                <PositionBadge position={r.position} />
              </span>
            </a>
          </li>
        ))}
      </ul>
      {shown < p.record.length && (
        <button className="more" onClick={() => setShown(shown + PAGE)}>
          {t.showMore}
        </button>
      )}
    </>
  );
}
