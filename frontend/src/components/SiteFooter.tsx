const REPO_URL = "https://github.com/LuisCastelblanco/amparo";

export default function SiteFooter() {
  return (
    <footer className="mt-12 border-t border-slate-200 bg-white">
      <div className="mx-auto w-full max-w-6xl space-y-2 px-4 py-6 text-sm text-slate-700 sm:px-6">
        <p>
          Built by Luis Castelblanco for the Nebius x NVIDIA Global AI Hackathon ·{" "}
          <a href={REPO_URL} target="_blank" rel="noreferrer" className="underline hover:text-slate-900">
            Source code on GitHub
          </a>
        </p>
        <p>Built with NVIDIA Nemotron on Nebius Token Factory and Tavily.</p>
        <p className="text-xs text-slate-600">
          Amparo is a drafting aid, not legal advice. Review the document before filing it and, if you can, check it
          with a lawyer, a university legal clinic (consultorio jurídico) or the Defensoría del Pueblo. Demo cases are
          synthetic.
        </p>
      </div>
    </footer>
  );
}
