import { api } from "../api";
import { Avatar, Status, shortChamber, termYears, useFetch } from "../common";
import { useI18n } from "../i18n";
import { href } from "../router";

export function Legislators({ q }: { q: string }) {
  const { t, num } = useI18n();
  const { data, loading, error } = useFetch(() => api.legislators(q), [q]);
  return (
    <section>
      <Status loading={loading && !data} error={error}>
        <p className="count">{t.legislatorsFound(num(data?.total ?? 0))}</p>
        <ul className="cards">
          {data?.legislators.map((p) => {
            const last = p.terms[p.terms.length - 1];
            return (
              <li key={p.id}>
                <a className="card" href={href(`/legislator/${p.id}`)}>
                  <Avatar name={p.name} photo={p.photo_url} size={48} />
                  <span>
                    <strong>{p.name}</strong>
                    {p.party && <small>{p.party}</small>}
                    {last && (
                      <small className="mono">
                        {shortChamber(last.chamber)} {termYears(last.start, last.end)}
                      </small>
                    )}
                  </span>
                </a>
              </li>
            );
          })}
        </ul>
      </Status>
    </section>
  );
}
