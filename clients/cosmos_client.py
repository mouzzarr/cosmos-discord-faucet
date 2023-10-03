import re
import subprocess
import json
import logging
from typing import List

from clients.faucet_client import FaucetClient, Balance, NodeStatus, NetworkDenomPair, TxInfo


class CosmosClient(FaucetClient):

    def execute(self, params, chain_id=True, json_output=True, json_node=True):
        params = [self.node_executable] + params
        if json_node:
            params.append(f"--node={self.node_rpc}")
        if chain_id:
            params.append(f"--chain-id={self.node_chain_id}")
        if json_output:
            params.append('--output=json')
        result = subprocess.run(params, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            result.check_returncode()
            if json_output:
                return json.loads(result.stdout)
            if result.stdout:
                return result.stdout
            return result.stderr
        except subprocess.CalledProcessError as cpe:
            output = str(result.stderr).split('\n', maxsplit=1)
            logging.error("Called Process Error: %s, stderr: %s", cpe, output)
            raise cpe

    def get_fixed_balance_denom(self, balance: Balance):
        if balance.denom.startswith('ibc/'):
            response = self.execute(["query", "ibc-transfer", "denom-trace", balance.denom])
            balance.original_denom = balance.denom
            balance.denom = response['denom_trace']['base_denom']
        return balance

    def get_balance(self, address: str, original_denom: str) -> Balance:
        """
        dymd query bank balances <address> <node> <chain-id>
        """
        try:
            response = self.execute(["query", "bank", "balances", address, f'--denom={original_denom}'], chain_id=False)
            return self.get_fixed_balance_denom(Balance(**response))
        except IndexError as index_error:
            logging.error('Parsing error on balance request: %s', index_error)
            raise index_error

    def get_node_status(self):
        """
        dymd status <node>
        """
        status = self.execute(["status"], chain_id=False, json_output=False)
        status = json.loads(status)
        try:
            node_status = NodeStatus(
                str(status['NodeInfo']['moniker']),
                str(status['NodeInfo']['network']),
                int(status['SyncInfo']['latest_block_height']),
                bool(status['SyncInfo']['catching_up'])
            )
            return node_status
        except KeyError as key:
            logging.error('Key not found in node status: %s', key)
            raise key

    def check_address(self, address: str):
        """
        dymd keys parse <address>
        """
        check = subprocess.run(
            [self.node_executable, "keys", "parse", f"{address}", '--output=json'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True)
        try:
            check.check_returncode()
            return json.loads(check.stdout[:-1])
        except subprocess.CalledProcessError as cpe:
            output = str(check.stderr).split('\n', maxsplit=1)
            logging.error("Called Process Error: %s, stderr: %s", cpe, output)
            raise cpe
        except IndexError as index_error:
            logging.error('Parsing error on address check: %s', index_error)
            raise index_error

    def tx_send(self, sender: str, recipient: str, amount: str, fees: int) -> str:
        """
        dymd tx bank send <from address> <to address> <amount> <fees> <node> <chain-id> --keyring-backend=test -y
        """
        response = self.execute([
            'tx',
            'bank',
            'send',
            sender,
            recipient,
            amount,
            f'--fees={fees}{self.node_denom}',
            '--keyring-backend=test',
            '-y'
        ])
        try:
            logging.info("Tx Send response %s", response)
            return response['txhash']
        except (TypeError, KeyError) as err:
            logging.critical('Could not read %s in tx response', err)
            raise err

    def fetch_bech32_address(self, address: str) -> str:
        if not address.startswith('0x'):
            return address

        response = self.execute(
            ['debug', 'addr', address.removeprefix('0x')], chain_id=False, json_output=False, json_node=False)
        match = re.search(r'Bech32 Acc: [^\s]+', response)
        if match:
            address = match.group().removeprefix('Bech32 Acc: ')

        return address

    def get_tx_info(self, hash_id: str) -> TxInfo:
        """
        dymd query tx <tx-hash> <node> <chain-id>
        """
        tx_response = self.execute(['query', 'tx', f'{hash_id}'])
        try:
            tx_body = tx_response['tx']['body']['messages'][0]
            height = int(tx_response['height'])
            if 'from_address' in tx_body.keys():
                tx_info = TxInfo(
                    height,
                    tx_body['from_address'],
                    tx_body['to_address'],
                    tx_body['amount'][0]['amount'] + tx_body['amount'][0]['denom'])
            elif 'sender' in tx_body.keys():
                tx_info = TxInfo(
                    height,
                    tx_body['sender'],
                    tx_body['receiver'],
                    tx_body['token']['amount'] + tx_body['token']['denom'])
            else:
                logging.error(
                    "Neither 'from_address' nor 'sender' key was found in response body:\n%s", tx_body)
                raise ValueError("Invalid tx response query")
            return tx_info
        except (TypeError, KeyError) as err:
            logging.critical('Could not read %s in raw log.', err)
            raise KeyError from err
