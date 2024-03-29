#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
# from time import time, sleep
from datetime import datetime #, timedelta
from fnmatch import fnmatch

import requests

TOKEN_ENV_VAR = 'ACTIONS_VERSION_CHECK_TOKEN'
API_BASE = 'https://api.github.com'

def get_timestamp():
    now = datetime.now()
    now_str = now.strftime('%Y-%m-%d_%H-%M-%S')
    return now_str

def get_token():
    return os.getenv(TOKEN_ENV_VAR)

def split_action(action):
    action_name, version = action.split('@')
    return action_name, version

def parse_args(now):
    parser = argparse.ArgumentParser(description='Check for outdated GitHub Actions', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-r', '--report', help='Path to report.json generated by stoe/action-reporting-cli', default="report.json", metavar='<path to report.json file>', type=argparse.FileType('r'))
    parser.add_argument('-ru', '--report-unique', help='Path to report-unique.json generated by stoe/action-reporting-cli', default="report-unique.json", metavar='<path to report-unique.json file>', type=argparse.FileType('r'))
    parser.add_argument('-c', '--csv', help='Path to csv file to write the results to', default=f"actions-version-check_{now}.csv", type=str)
    parser.add_argument('-j', '--json', help='Path to json file to write the results to', default=f"actions-version-check_{now}.json", type=str)
    parser.add_argument('-ar', '--allowed-actions-report', help='Path to text file containing allowed actions patterns that appear to not match any action usage', default=f"unused-allowed-actions-patterns_<org name or enterprise slug>_{now}.txt", type=str)
    parser.add_argument('-u', '--include-up-to-date', help='Include actions that are not used anywhere in outdated versions in the report', action='store_true', default=False)
    allowed_actions_patterns_sources = parser.add_mutually_exclusive_group()
    allowed_actions_patterns_sources.add_argument('-o', '--org', help='Compare used actions to actions allowed in GitHub organization <org name> to identify unused allowed actions patterns, requires token with admin:org scope in env var {TOKEN_ENV_VAR}', metavar='<organization name>', type=str)
    allowed_actions_patterns_sources.add_argument('-e', '--enterprise', help='Compare used actions to actions allowed in GitHub Enterprise <enterprise slug> to identify unused allowed actions patterns, requires token with admin:enterprise scope in env var {TOKEN_ENV_VAR}', metavar='<enterprise slug>', type=str)
    args = parser.parse_args()
    if args.org and not get_token():
        parser.error(f'-o requires token with admin:org scope in env var {TOKEN_ENV_VAR}')
    if args.enterprise and not get_token():
        parser.error(f'-e requires token with admin:enterprise scope in env var {TOKEN_ENV_VAR}')
    if args.include_up_to_date:
        print('Including actions that are not used in outdated versions anywhere in the report because -u was given')
    return args

def load_reports(report_file, report_unique_file):
    try:
        repos_report = json.load(report_file)
        unique_actions = json.load(report_unique_file)
    except json.decoder.JSONDecodeError as e:
        print(f'Error occurred while loading reports: {e}')
        sys.exit(1)
    finally:
        report_file.close()
        report_unique_file.close()
    return repos_report, unique_actions

def make_request(endpoint, token=get_token(), custom_headers=None):
    headers = {"Accept": "application/vnd.github+json",  "X-GitHub-Api-Version": "2022-11-28"}
    url = f'{API_BASE}{endpoint}'
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if custom_headers:
        headers.update(custom_headers)
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 403:# and 'X-RateLimit-Reset' in response.headers:
            print(f"Org/Enterprise might not exist or token is not valid for it: {url}")
            sys.exit(1)
            #reset_time = int(response.headers['X-RateLimit-Reset'])
            #current_time = int(time())
            #sleep_time = reset_time - current_time
            #now = datetime.now()
            #resume_time = now + timedelta(seconds=sleep_time)
            #print(f'Reached {API_BASE} API rate limit at {now.strftime("%Y-%m-%d %H:%M:%S")}. Sleeping for {sleep_time} seconds ({round(sleep_time / 60)} minutes) until {resume_time.strftime("%Y-%m-%d %H:%M:%S")}. Hit Ctrl+C to exit.')            
            #sleep(sleep_time)
            #return make_request(endpoint, token, custom_headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f'Error occurred while making the request: {e}')
        sys.exit(1)
    except KeyboardInterrupt:
        print('\nKeyboard interrupt detected, exiting...')
        sys.exit(1)

def get_actions_versions(unique_actions, include_up_to_date):
    actions_versions = {}
    print('Fetching latest releases for actions')
    for action in unique_actions:
        action_name, version = split_action(action)
        if not action_name in actions_versions:
            actions_versions[action_name] = {}
            actions_versions[action_name]["versions_used_in_repos"] = {}
            print(f'    Fetching latest release for {action_name}')  
            response = make_request(f'/repos/{action_name}/releases/latest')
            try:
                actions_versions[action_name]["latest_release"] = response['tag_name']
            except KeyError:
                print(f'    No releases found for {action_name} - skipping this action...')
                actions_versions.pop(action_name)
                continue
        if not version in actions_versions[action_name]["versions_used_in_repos"]:
            if not include_up_to_date and version == actions_versions[action_name]["latest_release"]:
                continue
            actions_versions[action_name]["versions_used_in_repos"][version] = []
    #print(actions_versions)
    return actions_versions

def get_repo_usage(repos_report, actions_versions, org):
    for repo in repos_report:
        for action in repo["uses"]:
            action_name, version = split_action(action)
            try:
                if org:
                    if repo["owner"] != org:
                        continue
                actions_versions[action_name]["versions_used_in_repos"][version].append(f'{repo["owner"]}/{repo["repo"]}')
            except KeyError:
                pass
    return actions_versions

def get_allowed_actions(org_name=None, enterprise_slug=None):
    if org_name:
        print(f'Fetching allowed actions for GitHub organization {org_name}')
        response = make_request(f'/orgs/{org_name}/actions/permissions/selected-actions')
    elif enterprise_slug:
        print(f'Fetching allowed actions for GitHub Enterprise {enterprise_slug}')
        response = make_request(f'/enterprises/{enterprise_slug}/actions/permissions/selected-actions')
    allowed_actions = response["patterns_allowed"]
    for pattern in allowed_actions:
        if re.match(r'.*\.yml(@.*)$', pattern):
            # Remove patterns directly referencing reusable workflows
            allowed_actions.remove(pattern)
    if org_name:
        print(f'    Found {len(allowed_actions)} allowed actions patterns in GitHub organization {org_name} (excluding patterns that directly reference reusable workflows)')
    elif enterprise_slug:
        print(f'    Found {len(allowed_actions)} allowed actions patterns in GitHub Enterprise {enterprise_slug} (excluding patterns that directly reference reusable workflows)')
    return allowed_actions

def compare_allowed_actions(allowed_actions_patterns, actions_versions, allowed_actions_report, source_name):
    print('Comparing allowed actions patterns to used actions')
    allowed_actions_report = allowed_actions_report.replace('<org name or enterprise slug>', source_name)
    with open(allowed_actions_report, 'w') as allowed_actions_report:
        matching_patterns = 0
        allowed_actions_report.write(f'Allowed actions patterns that do not *seem* to match any used actions:\n')
        for pattern in allowed_actions_patterns:
            # check whether pattern fnmatches any action in unique_actions
            match = False
            for action in actions_versions.keys():
                if fnmatch(action, pattern):
                    match = True
                    break
            if match:
                matching_patterns += 1
            else:
                allowed_actions_report.write(f'{pattern}\n')
    if len(allowed_actions_patterns) == matching_patterns:
        print('    All allowed actions patterns match used actions, not writing report')
    else:
        print(f"    Found {matching_patterns} allowed actions pattern(s) that match(es) at least one used actions, wrote {len(allowed_actions_patterns) - matching_patterns} pattern(s) that don't seem to match any used actions to {allowed_actions_report.name}")

def clean_output(actions_versions_with_repo_usage):
    # Remove actions that don't have any repo usage (because they are not used in an outdated version -o was given)
    for action in list(actions_versions_with_repo_usage.keys()):  # Create a copy of the keys
        for version in list(actions_versions_with_repo_usage[action]["versions_used_in_repos"].keys()):
            # Pop the version if it doesn't have any repo usage (because it's not in scope for org given in -o)
            if not actions_versions_with_repo_usage[action]["versions_used_in_repos"][version]:
                actions_versions_with_repo_usage[action]["versions_used_in_repos"].pop(version)
        if not actions_versions_with_repo_usage[action]["versions_used_in_repos"]:
            # Pop the action if it doesn't have any versions with repo usage
            actions_versions_with_repo_usage.pop(action)
    return actions_versions_with_repo_usage

def write_outdated_actions_csv(csv_location, actions_versions):
    print(f'Writing CSV report to {csv_location}')
    with open(csv_location, 'w') as f:
        f.write("action_name,latest_release,used_version,used_in_repos\n")
        for action_name in actions_versions:
            for version in actions_versions[action_name]["versions_used_in_repos"]:
                f.write(f'{action_name},{actions_versions[action_name]["latest_release"]},{version},{";".join(actions_versions[action_name]["versions_used_in_repos"][version])}\n')

def write_outdated_actions_json(json_location, actions_versions):
    print(f'Writing JSON report to {json_location}')
    with open(json_location, 'w') as f:
        json.dump(actions_versions, f)

def main():
    now = get_timestamp()
    args = parse_args(now)
    repos_report, unique_actions = load_reports(args.report, args.report_unique)
    actions_versions = get_actions_versions(unique_actions, args.include_up_to_date)
    #print(actions_versions)
    actions_versions_with_repo_usage = get_repo_usage(repos_report, actions_versions, args.org)
    cleaned_up_actions_versions = clean_output(actions_versions_with_repo_usage)
    
    if args.csv:
        write_outdated_actions_csv(args.csv, cleaned_up_actions_versions)
    if args.json:
        write_outdated_actions_json(args.json, cleaned_up_actions_versions)
    
    if args.org:
        allowed_actions = get_allowed_actions(org_name=args.org)
        compare_allowed_actions(allowed_actions, cleaned_up_actions_versions, args.allowed_actions_report, args.org)
    elif args.enterprise:
        allowed_actions = get_allowed_actions(enterprise_slug=args.enterprise)
        compare_allowed_actions(allowed_actions, cleaned_up_actions_versions, args.allowed_actions_report, args.enterprise)
    else:
        print('Not comparing allowed actions to used actions because neither -o nor -e was given')
    

if __name__ == '__main__':
    main()
