import unittest
from unittest.mock import Mock, patch

import create_test_network as n


class NetworkTests(unittest.TestCase):
    @patch('create_test_network.remote')
    def test_csv_parses_embedded_commas(self, remote):
        remote.return_value = 'Name,Uplinks,\n"lab, one",,\n'
        self.assertEqual(n.query(Mock(), 'list')[0]['Name'], 'lab, one')

    @patch('create_test_network.verify')
    @patch('create_test_network.remote')
    @patch('create_test_network.query', side_effect=[[], []])
    def test_creates_isolated_switch_and_inherited_portgroup(self, query, remote, verify):
        client = Mock()
        n.create_network(client, 'lab-01')
        commands = [call.args[1:] for call in remote.call_args_list]
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[0], ('esxcli', 'network', 'vswitch', 'standard', 'add', '--vswitch-name', 'lab-01'))
        for flag in ('--allow-promiscuous', '--allow-mac-change', '--allow-forged-transmits'):
            self.assertEqual(commands[1][commands[1].index(flag)+1], 'true')
        self.assertEqual(commands[2], ('esxcli', 'network', 'vswitch', 'standard', 'portgroup', 'add',
                                    '--portgroup-name', 'lab-01', '--vswitch-name', 'lab-01'))
        verify.assert_called_once_with(client, 'lab-01')

    @patch('create_test_network.remote')
    @patch('create_test_network.query')
    def test_name_conflicts_make_no_changes(self, query, remote):
        for switches, groups in (([{'Name': 'lab'}], []), ([], [{'Name': 'lab'}])):
            query.side_effect = [switches, groups]
            with self.assertRaises(n.CloneError):
                n.create_network(Mock(), 'lab')
        remote.assert_not_called()

    @patch('create_test_network.remote')
    @patch('create_test_network.query', side_effect=[[], []])
    def test_security_failure_does_not_add_portgroup_or_delete_resources(self, query, remote):
        remote.side_effect = ['', n.CloneError('denied')]
        with self.assertRaises(n.CloneError):
            n.create_network(Mock(), 'lab')
        self.assertEqual(remote.call_count, 2)

    def rows(self):
        return [[{'Name': 'lab', 'Uplinks': ''}],
                [{'Name': 'lab', 'VirtualSwitch': 'lab', 'VLANID': '0'}],
                [{'AllowPromiscuous': 'true', 'AllowMACAddressChange': 'true', 'AllowForgedTransmits': 'true'}],
                [{'OverrideVswitchAllowPromiscuous': 'false'}],
                [{'OverrideVswitchEnabled': 'false'}],
                [{'OverrideVswitchUplinks': 'false'}]]

    @patch('create_test_network.query')
    def test_verifies_all_inherited_policies(self, query):
        query.side_effect = self.rows()
        n.verify(Mock(), 'lab')
        self.assertEqual(query.call_count, 6)

    @patch('create_test_network.query')
    def test_uplink_or_disabled_security_or_policy_override_fails(self, query):
        for failure in ('uplink', 'security', 'override'):
            rows = self.rows()
            if failure == 'uplink':
                rows[0][0]['Uplinks'] = 'vmnic0,'
            elif failure == 'security':
                rows[2][0]['AllowPromiscuous'] = 'false'
            else:
                rows[5][0]['OverrideVswitchUplinks'] = 'true'
            query.side_effect = rows
            with self.assertRaises(n.CloneError):
                n.verify(Mock(), 'lab')

    def test_only_name_is_required(self):
        args = n.parser().parse_args(['lab-01'])
        self.assertEqual(args.name, 'lab-01')
        self.assertIsNone(args.creds_file)


if __name__ == '__main__':
    unittest.main()
