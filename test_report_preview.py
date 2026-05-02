"""Run locally to preview today's or yesterday's report without posting to Slack.

Usage:
    python test_report_preview.py            # yesterday
    python test_report_preview.py today      # today (partial)
"""
import json
import sys
from datetime import datetime, timedelta

import config
import gmail_client
import slack_client


def main() -> None:
    config.validate()
    today_local = datetime.now(config.REPORT_TIMEZONE).date()
    if len(sys.argv) > 1 and sys.argv[1] == "today":
        report_date = today_local
    else:
        report_date = today_local - timedelta(days=1)

    print(f"Previewing report for {report_date}\n")
    stats = gmail_client.collect_reply_stats(report_date)

    print(f"  Total replies: {stats.total_replies}")
    print(f"  Avg response:  {stats.avg_response_human}")
    print(f"  Sample size:   {stats.sample_size}")
    print("\nSlack payload:\n")
    print(json.dumps(slack_client.build_blocks(stats, report_date), indent=2))


if __name__ == "__main__":
    main()
