/** Help: a short guide through the installer, and answers to the questions
    people get stuck on. Opened from the "Help" button in the header. */

const GUIDE: { title: string; text: string }[] = [
  { title: "Saved answers", text: "Choose a passphrase. It encrypts everything you type, so a later run can upgrade or remove the install." },
  { title: "Where it runs", text: "A cluster on this computer (minikube), a server you reach over SSH, or Amazon EKS, which Terraform creates for you." },
  { title: "Repositories", text: "Keep DevOpsCourse22, paste a GitHub token, press Check access. Every line should be green." },
  { title: "AI model", text: "The model reviews use by default: a model server on your network, one on your tailnet, or a cloud provider with its key." },
  { title: "Admins", text: "GitHub usernames that may change the app's settings." },
  { title: "Main menu", text: "Check the summary and press Deploy. Later, the same page shows your install, its links, Upgrade and Remove." },
  { title: "Deploy", text: "Follow the log. When it says \"Your turn\", do what the card asks (for example create the GitHub OAuth App) and press Continue." },
];

const FAQ: { q: string; a: string }[] = [
  { q: "What do I need before I start?",
    a: "Docker, a GitHub token with repo and admin:repo_hook, and either a running minikube with at least 8 GB of memory or an AWS account." },
  { q: "I forgot my passphrase.",
    a: "It cannot be recovered. On the first screen choose Start over and answer again. A cluster or AWS install that already exists stays as it is; use Remove on it after the new answers point at it." },
  { q: "My cluster is not in the list on \"Where it runs\".",
    a: "The installer reads every kubeconfig in your .kube folder. Start the cluster first (minikube start), then restart the installer. For minikube, add --network minikube (or --network <profile>) to docker run." },
  { q: "\"Check access\" shows red lines.",
    a: "The token cannot see or change that repository. Create a classic token with repo and admin:repo_hook, and make sure your GitHub user has access to the DevOpsCourse22 repositories." },
  { q: "What is the GitHub OAuth App and why do I make it?",
    a: "It lets people sign in to the app with GitHub. GitHub has no way to create it automatically, so the installer shows the exact two addresses to paste on github.com/settings/applications/new, then asks for its client ID and secret." },
  { q: "The deploy waits for \"minikube tunnel\".",
    a: "On minikube the app is reached through a tunnel. Open a second terminal as administrator, run the command the card shows (minikube tunnel, or minikube tunnel -p <profile>), leave it open and press Continue." },
  { q: "The first build takes very long.",
    a: "Jenkins runs the tests, builds the images, scans them with Trivy and pushes them, one repository after another. 20 to 40 minutes is normal the first time. Later builds are faster." },
  { q: "A step failed. What now?",
    a: "Read the red message and the log under it, fix what it says, then Back to review and press Deploy (or Upgrade) again. Steps that already finished are safe to repeat." },
  { q: "How do I open Jenkins, Argo CD and Grafana?",
    a: "Press \"Jenkins, Argo CD, Grafana\" at the top. All three sign in as admin with the one password shown there. Locally they open on localhost:8081 to 8083; on EKS each has its own AWS address." },
  { q: "A review says the repository could not be cloned.",
    a: "Private repositories need you to sign in to the app with GitHub first; the app uses your own GitHub access to read them. Sign in, then press Resume on the review." },
  { q: "The main menu says my install is not running.",
    a: "The installer checks the cluster itself. If it is gone, use Remove to clean up what is left (on EKS this also deletes the AWS resources), or Forget if you know nothing is left." },
  { q: "How do I stop paying for EKS?",
    a: "Main menu, type destroy and press Remove. It deletes the app, the load balancers and everything Terraform created. Keep the installer's Docker volume (ai-scan-installer) until Remove has finished." },
  { q: "Where are my secrets kept?",
    a: "In the installer's memory and in answers.enc on its Docker volume, encrypted with your passphrase. In the cluster they become Kubernetes Secrets. They are never written to git or shown in the log." },
  { q: "Can I use my own HTTPS certificate?",
    a: "Yes. On \"Where it runs\", Add an HTTPS certificate, choose the certificate and its private key, set the app address to https://<a name it covers> and press Check certificate." },
];

export default function Help({ onClose }: { onClose: () => void }) {
  return (
    <section className="help" aria-labelledby="help-h">
      <div className="services-head">
        <h2 id="help-h">Help</h2>
        <button type="button" className="link" onClick={onClose}>Close</button>
      </div>

      <h3>How it works</h3>
      <ol className="help-guide">
        {GUIDE.map((g) => (
          <li key={g.title}><strong>{g.title}.</strong> {g.text}</li>
        ))}
      </ol>

      <h3>Questions</h3>
      <div className="faq">
        {FAQ.map((f) => (
          <details key={f.q}>
            <summary>{f.q}</summary>
            <p>{f.a}</p>
          </details>
        ))}
      </div>
      <p className="hint">More detail: the README in the ai-scanner-installer repository.</p>
    </section>
  );
}
