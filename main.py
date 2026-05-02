"""Daily entry point. Computes yesterday's stats and posts to Slack."""
from datetime import datetime, timedelta

import config
import gmail_client
import slack_client


def main() -> None:
    config.validate()
    # We run at 9am local — report covers the prior calendar day.
    today_local = datetime.now(config.REPORT_TIMEZONE).date()
    report_date = today_local - timedelta(days=1)

    print(f"Generating report for {report_date} ({config.REPORT_TIMEZONE})")
    stats = gmail_client.collect_reply_stats(report_date)
    print(f"Replies: {stats.total_replies} | Avg: {stats.avg_response_human} "
          f"| Sample: {stats.sample_size}")

    slack_client.send_report(stats, report_date)
    print("Report sent.")


if __name__ == "__main__":
    main()
