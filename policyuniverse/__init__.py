from __future__ import print_function
import copy
import fnmatch
import json
import os
import sys
import logging


# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

master_permissions_path = os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    'master_permissions.json')

administrator_access_path = os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    'action_categories.json')

# To update the master_permissions.json, open this URL in chrome:
# https://awspolicygen.s3.amazonaws.com/policygen.html
# Bring up the console from the inspector and type:
# JSON.stringify(app.PolicyEditorConfig.serviceMap)
# Copy and Paste the output into a file and then reformat that JSON with this code:

# with open(".../new.json") as json_data:
#     blah = json.load(json_data)
#
# with open('.../master_permissions.json', 'w') as outfile:
#     json.dump(blah, outfile, indent=2, sort_keys=True)

# Make sure indent=2 and sort_keys=True.

# NEW 10/30/2017:
# To update the action_categories.json (for action grouping/classification),
# Attach the "AdministratorAccess" managed policy to an IAM Role and with the inspector open,
# browse to the policy summary.  Find the REST call made to this URL:
# https://console.aws.amazon.com/iam/api/policies/arn:aws:iam::aws:policy%C2%B6AdministratorAccess/version/v1/policysummary
# Download the contents of that URL to a file called action_categories_new.json
# Start an interactive python session:
# > import policyuniverse
# > policyuniverse.action_categories.reformat_console_data('action_categories_new.json')
# > CTRL-D
# Delete action_categories_new.json and submit a PR with the chagnes to action_categories.json


global_permissions = json.load(open(master_permissions_path, 'r'))
action_categories = json.load(open(administrator_access_path, 'r'))
all_permissions = set()

for technology_name in global_permissions:
    technology_prefix = global_permissions[technology_name]["StringPrefix"]
    for action in global_permissions[technology_name]["Actions"]:
        all_permissions.add("{}:{}".format(technology_prefix, action.lower()))

policy_headers = ['rolepolicies', 'grouppolicies', 'userpolicies', 'policy']


def expand_minimize_over_policies(policies, activity, **kwargs):
    for header in policy_headers:
        if header in policies:
            output = {header: {}}
            for policy in policies[header]:
                output[header][policy] = activity(policy=policies[header][policy], **kwargs)
            return output

    return activity(policy=policies, **kwargs)


def _get_prefixes_for_action(action):
    """
    :param action: iam:cat
    :return: [ "iam:", "iam:c", "iam:ca", "iam:cat" ]
    """
    (technology, permission) = action.split(':')
    retval = ["{}:".format(technology)]
    phrase = ""
    for char in permission:
        newphrase = "{}{}".format(phrase, char)
        retval.append("{}:{}".format(technology,newphrase))
        phrase = newphrase
    return retval


def _expand_wildcard_action(action):
    """
    :param action: 'autoscaling:*'
    :return: A list of all autoscaling permissions matching the wildcard
    """
    if isinstance(action, list):
        expanded_actions = []
        for item in action:
            expanded_actions.extend(_expand_wildcard_action(item))
        return expanded_actions

    else:
        if '*' in action:
            expanded = [
                expanded_action.lower() for expanded_action in all_permissions if fnmatch.fnmatchcase(
                    expanded_action.lower(), action.lower()
                )
            ]

            # if we get a wildcard for a tech we've never heard of, just return the wildcard
            if not expanded:
                return [action.lower()]

            return expanded
        return [action.lower()]


def _get_desired_actions_from_statement(statement):
    desired_actions = set()
    actions = _expand_wildcard_action(statement['Action'])

    for action in actions:
        if action not in all_permissions:
            raise Exception("Desired action not found in master permission list. {}".format(action))
        desired_actions.add(action)

    return desired_actions


def _get_denied_prefixes_from_desired(desired_actions):
    denied_actions = all_permissions.difference(desired_actions)
    denied_prefixes = set()
    for denied_action in denied_actions:
        for denied_prefix in _get_prefixes_for_action(denied_action):
            denied_prefixes.add(denied_prefix)

    return denied_prefixes


def _check_min_permission_length(permission, minchars=None):
    if minchars and len(permission) < int(minchars) and permission != '':
        print("Skipping prefix {} because length of {}".format(permission, len(permission)) , file = sys.stderr)
        return True
    return False


def minimize_statement_actions(statement, minchars=None):
    minimized_actions = set()

    if statement['Effect'] != 'Allow':
        raise Exception("Minification does not currently work on Deny statements.")

    desired_actions = _get_desired_actions_from_statement(statement)
    denied_prefixes = _get_denied_prefixes_from_desired(desired_actions)

    for action in desired_actions:
        if action in denied_prefixes:
            print("Action is a denied prefix. Action: {}".format(action))
            minimized_actions.add(action)
            continue

        found_prefix = False
        prefixes = _get_prefixes_for_action(action)
        for prefix in prefixes:

            permission = prefix.split(':')[1]
            if _check_min_permission_length(permission, minchars=minchars):
                continue

            if prefix not in denied_prefixes:
                if prefix not in desired_actions:
                    prefix = "{}*".format(prefix)
                minimized_actions.add(prefix)
                found_prefix = True
                break

        if not found_prefix:
            print("Could not suitable prefix. Defaulting to {}".format(prefixes[-1]))
            minimized_actions.add(prefixes[-1])

    # sort the actions
    minimized_actions_list = list(minimized_actions)
    minimized_actions_list.sort()

    return minimized_actions_list


def get_actions_from_statement(statement):
    allowed_actions = set()

    if not type(statement.get('Action', [])) == list:
        statement['Action'] = [statement['Action']]

    for action in statement.get('Action', []):
        allowed_actions = allowed_actions.union(set(_expand_wildcard_action(action)))

    if not type(statement.get('NotAction', [])) == list:
        statement['NotAction'] = [statement['NotAction']]

    inverted_actions = set()
    for action in statement.get('NotAction', []):
        inverted_actions = inverted_actions.union(set(_expand_wildcard_action(action)))

    if inverted_actions:
        actions = _invert_actions(inverted_actions)
        allowed_actions = allowed_actions.union(actions)

    return allowed_actions


def _invert_actions(actions):
    from policyuniverse import all_permissions
    return all_permissions.difference(actions)


def expand_policy(policy=None, expand_deny=False):
    # Perform a deepcopy to avoid mutating the input
    result = copy.deepcopy(policy)

    if type(result['Statement']) is dict:
        result['Statement'] = [result['Statement']]
    for statement in result['Statement']:
        if statement['Effect'].lower() == 'deny' and not expand_deny:
            continue
        actions = get_actions_from_statement(statement)
        if 'NotAction' in statement:
            del statement['NotAction']
        statement['Action'] = sorted(list(actions))

    return result


def minimize_policy(policy=None, minchars=None):

    str_pol = json.dumps(policy, indent=2)
    size = len(str_pol)

    for statement in policy['Statement']:
        minimized_actions = minimize_statement_actions(statement, minchars=minchars)
        statement['Action'] = minimized_actions

    str_end_pol = json.dumps(policy, indent=2)
    end_size = len(str_end_pol)

    # print str_end_pol
    print("Start size: {}. End size: {}".format(size, end_size), file = sys.stderr)
    return policy
