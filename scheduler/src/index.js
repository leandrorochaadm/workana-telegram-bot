// Triggers the bots' GitHub Actions workflows on a precise schedule.
// GitHub's own `schedule` event is delayed or dropped under load, so a
// Cloudflare Cron Trigger (every 15 min, UTC) calls the workflow_dispatch API
// instead, and each job decides in Brasília time (UTC-3) whether to run.

const MAX_ATTEMPTS = 3;
const TIME_ZONE = "America/Sao_Paulo";

// weekday: 0 = Sunday ... 6 = Saturday; hour/minute in Brasília time
const JOBS = [
  {
    name: "workana",
    repo: "leandrorochaadm/workana-telegram-bot",
    workflow: "monitor.yml",
    // Every 15 min, all day
    shouldRun: () => true,
  },
  {
    name: "gupy",
    repo: "leandrorochaadm/telegram-vagas-gupy-bot",
    workflow: "vagas.yml",
    shouldRun: ({ weekday, hour, minute }) => {
      const isWeekend = weekday === 0 || weekday === 6;
      if (isWeekend) return hour >= 10 && hour <= 18 && minute === 0; // hourly, 10h to 18h
      return hour >= 8 && hour <= 20 && minute % 30 === 0; // every 30 min, 8h to 20h30
    },
  },
];

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function brasiliaTime(timestamp) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: TIME_ZONE,
      weekday: "short",
      hour: "numeric",
      minute: "numeric",
      hourCycle: "h23",
    })
      .formatToParts(new Date(timestamp))
      .map(({ type, value }) => [type, value]),
  );
  const minute = Number(parts.minute);
  return {
    weekday: WEEKDAYS.indexOf(parts.weekday),
    hour: Number(parts.hour),
    // Snap to the 15-min slot so a late cron still matches its schedule
    minute: minute - (minute % 15),
  };
}

async function dispatchWorkflow(env, job) {
  const url = `https://api.github.com/repos/${job.repo}/actions/workflows/${job.workflow}/dispatches`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "bots-scheduler",
    },
    body: JSON.stringify({ ref: "main" }),
  });

  if (!response.ok) {
    const body = await response.text();
    const error = new Error(`${job.name}: GitHub API ${response.status}: ${body}`);
    // 4xx means bad token/config: retrying will not help
    error.retryable = response.status >= 500 || response.status === 429;
    throw error;
  }
}

async function dispatchWithRetry(env, job) {
  for (let attempt = 1; ; attempt++) {
    try {
      await dispatchWorkflow(env, job);
      console.log(`${job.name}: workflow dispatched (attempt ${attempt})`);
      return;
    } catch (error) {
      const retryable = error.retryable !== false;
      if (!retryable || attempt >= MAX_ATTEMPTS) throw error;
      console.warn(`${job.name}: attempt ${attempt} failed, retrying: ${error.message}`);
      await new Promise((resolve) => setTimeout(resolve, attempt * 5000));
    }
  }
}

export default {
  async scheduled(controller, env) {
    const now = brasiliaTime(controller.scheduledTime);
    const due = JOBS.filter((job) => job.shouldRun(now));
    console.log(`Cron fired: ${JSON.stringify(now)} -> ${due.map((job) => job.name).join(", ") || "nothing"}`);

    // One job failing must not stop the others; still surface it as an error in the logs
    const results = await Promise.allSettled(due.map((job) => dispatchWithRetry(env, job)));
    const reasons = results.filter((result) => result.status === "rejected").map((result) => result.reason);
    if (reasons.length) throw new AggregateError(reasons, reasons.map((reason) => reason.message).join(" | "));
  },
};
