const SOURCE_URL =
  "https://www.infobae.com/colombia/2026/04/23/defensoria-del-pueblo-reporto-incremento-de-18-en-tutelas-para-acceder-a-la-salud-llamo-a-una-reforma-urgente/";

const HOW_IT_WORKS = [
  "Tell your case in your own words, in Spanish.",
  "Amparo extracts the facts, finds Constitutional Court rulings that apply and drafts the tutela.",
  "Each citation is checked; anything that cannot be verified is removed before you see it.",
];

/** Short orientation for a first-time visitor, shown above the form. */
export default function Intro() {
  return (
    <section aria-labelledby="intro-heading" className="mb-8 grid gap-4 md:grid-cols-3">
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 id="intro-heading" className="text-xs font-semibold uppercase tracking-wide text-slate-600">
          The problem
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-slate-800">
          ~312,500 health tutelas were filed in Colombia in 2025 (+18% vs 2024), and the poorest departments file the
          fewest.
        </p>
        <p className="mt-2 text-xs text-slate-600">
          Source:{" "}
          <a href={SOURCE_URL} target="_blank" rel="noreferrer" className="underline hover:text-slate-900">
            Defensoría del Pueblo, April 2026 report
          </a>{" "}
          (via a news report).
        </p>
      </div>
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-600">How it works</h2>
        <ol className="mt-2 list-decimal space-y-1 pl-4 text-sm leading-relaxed text-slate-800">
          {HOW_IT_WORKS.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ol>
        <p className="mt-2 text-sm font-medium text-emerald-800">
          Every cited ruling is verified against the Constitutional Court&apos;s website.
        </p>
      </div>
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-600">What you get</h2>
        <ul className="mt-2 list-disc space-y-1 pl-4 text-sm leading-relaxed text-slate-800">
          <li>A tutela document ready to review and file, as .docx or PDF.</li>
          <li>The verified rulings it cites, with their source.</li>
          <li>A checklist of annexes and where to file it.</li>
        </ul>
      </div>
    </section>
  );
}
