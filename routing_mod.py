# -*- coding: utf-8; -*-
#
# The MIT License (MIT)
#
# Copyright (c) 2014 Flavien Charlon
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import asyncio
import bitcoin.base58
import bitcoin.core
import bitcoin.core.serialize
import bitcoin.core.script
import bitcoin.rpc
import bitcoin.wallet
import colorcore.addresses
import colorcore.routing
import decimal
import itertools
import math
import openassets.protocol
import openassets.transactions

import binascii
import json
import http.client as httplib
from bitcoin.core import COIN, lx, b2lx, CBlock, CTransaction, COutPoint, CTxOut
from bitcoin.core.script import CScript
from bitcoin.wallet import CBitcoinAddress


class Controller(object):
    """Contains all operations provided by Colorcore."""

    def __init__(self, configuration, cache_factory, tx_parser, event_loop):
        self.configuration = configuration
        self.provider = configuration.create_blockchain_provider(event_loop)
        self.tx_parser = tx_parser
        self.cache_factory = cache_factory
        self.event_loop = event_loop
        self.convert = Convert(configuration.asset_byte)

    @asyncio.coroutine
    def getbalance(self,
        address: "Obtain the balance of this address only, or all addresses if unspecified"=None,
        minconf: "The minimum number of confirmations (inclusive)"='1',
        maxconf: "The maximum number of confirmations (inclusive)"='9999999'
    ):
        """Obtains the balance of the wallet or an address."""
        from_address = self._as_any_address(address) if address is not None else None
        unspent_outputs = yield from self._get_unspent_outputs(
            from_address, min_confirmations=self._as_int(minconf), max_confirmations=self._as_int(maxconf))
        colored_outputs = [output.output for output in unspent_outputs]

        sorted_outputs = sorted(colored_outputs, key=lambda output: output.script)
        output_groups = [
            (script, list(group)) for (script, group)
            in itertools.groupby(sorted_outputs, lambda output: output.script)]

        if not output_groups and address is not None:
            output_groups.append((from_address.to_scriptPubKey(), []))

        table = []
        for script, script_outputs in output_groups:
            total_value = self.convert.to_coin(sum([item.value for item in script_outputs]))

            address = self.convert.script_to_address(script)
            if address is not None:
                oa_address = str(colorcore.addresses.Base58Address(
                    address, address.nVersion, self.configuration.namespace))
            else:
                oa_address = None

            group_details = {
                'address': self.convert.script_to_display_string(script),
                'oa_address': oa_address,
                'value': total_value,
                'assets': []
            }

            table.append(group_details)

            sorted_script_outputs = sorted(
                [item for item in script_outputs if item.asset_id],
                key=lambda output: output.asset_id)

            for asset_id, outputs in itertools.groupby(sorted_script_outputs, lambda output: output.asset_id):
                total_quantity = sum([item.asset_quantity for item in outputs])
                group_details['assets'].append({
                    'asset_id': self.convert.asset_id_to_base58(asset_id),
                    'quantity': str(total_quantity)
                })

        return table

    @asyncio.coroutine
    def listunspent(self,
        address: "Obtain the balance of this address only, or all addresses if unspecified"=None,
        minconf: "The minimum number of confirmations (inclusive)"='1',
        maxconf: "The maximum number of confirmations (inclusive)"='9999999'
    ):
        """Returns an array of unspent transaction outputs augmented with the asset ID and quantity of
        each output."""
        from_address = self._as_any_address(address) if address is not None else None
        unspent_outputs = yield from self._get_unspent_outputs(
            from_address, min_confirmations=self._as_int(minconf), max_confirmations=self._as_int(maxconf))

        table = []
        for output in unspent_outputs:
            parsed_address = self.convert.script_to_address(output.output.script)
            if parsed_address is not None:
                oa_address = str(colorcore.addresses.Base58Address(
                    parsed_address, parsed_address.nVersion, self.configuration.namespace))
            else:
                oa_address = None

            table.append({
                'txid': bitcoin.core.b2lx(output.out_point.hash),
                'vout': output.out_point.n,
                'address': self.convert.script_to_display_string(output.output.script),
                'oa_address': oa_address,
                'script': bitcoin.core.b2x(output.output.script),
                'amount': self.convert.to_coin(output.output.value),
                'confirmations': output.confirmations,
                'asset_id':
                    None if output.output.asset_id is None
                    else self.convert.asset_id_to_base58(output.output.asset_id),
                'asset_quantity': str(output.output.asset_quantity)
            })

        return table

    @asyncio.coroutine
    def sendbitcoin(self,
        address: "The address to send the bitcoins from",
        amount: "The amount of satoshis to send",
        to: "The address to send the bitcoins to",
        fees: "The fess in satoshis for the transaction"=None,
        mode: """'broadcast' (default) for signing and broadcasting the transaction,
            'signed' for signing the transaction without broadcasting,
            'unsigned' for getting the raw unsigned transaction without broadcasting"""='broadcast'
    ):
        """Creates a transaction for sending bitcoins from an address to another."""
        from_address = self._as_any_address(address)
        to_address = self._as_any_address(to)

        builder = openassets.transactions.TransactionBuilder(self.configuration.dust_limit)
        colored_outputs = yield from self._get_unspent_outputs(from_address)

        transfer_parameters = openassets.transactions.TransferParameters(
            colored_outputs, to_address.to_scriptPubKey(), from_address.to_scriptPubKey(),
            self._as_int(amount))
        transaction = builder.transfer_bitcoin(transfer_parameters, self._get_fees(fees))

        return self.tx_parser((yield from self._process_transaction(transaction, mode)))

    @asyncio.coroutine
    def sendasset(self,
        address: "The address to send the asset from",
        asset: "The asset ID identifying the asset to send",
        amount: "The amount of asset units to send",
        to: "The address to send the asset to",
        fees: "The fess in satoshis for the transaction"=None,
        mode: """'broadcast' (default) for signing and broadcasting the transaction,
            'signed' for signing the transaction without broadcasting,
            'unsigned' for getting the raw unsigned transaction without broadcasting"""='broadcast'
    ):
        """Creates a transaction for sending an asset from an address to another."""
        from_address = self._as_any_address(address)
        to_address = self._as_openassets_address(to)

        builder = openassets.transactions.TransactionBuilder(self.configuration.dust_limit)
        colored_outputs = yield from self._get_unspent_outputs(from_address)

        transfer_parameters = openassets.transactions.TransferParameters(
            colored_outputs, to_address.to_scriptPubKey(), from_address.to_scriptPubKey(), self._as_int(amount))

        transaction = builder.transfer_assets(
            self.convert.base58_to_asset_id(asset), transfer_parameters, from_address.to_scriptPubKey(),
            self._get_fees(fees))

        return self.tx_parser((yield from self._process_transaction(transaction, mode)))

    @asyncio.coroutine
    def issueasset(self,
        address: "The address to issue the asset from",
        amount: "The amount of asset units to issue",
        to: "The address to send the asset to; if unspecified, the assets are sent back to the issuing address"=None,
        metadata: "The metadata to embed in the transaction"='',
        fees: "The fess in satoshis for the transaction"=None,
        mode: """'broadcast' (default) for signing and broadcasting the transaction,
            'signed' for signing the transaction without broadcasting,
            'unsigned' for getting the raw unsigned transaction without broadcasting"""='broadcast'
    ):
        """Creates a transaction for issuing an asset."""
        from_address = self._as_any_address(address)

        if to is None:
            to_address = self._as_any_address(address)
        else:
            to_address = self._as_openassets_address(to)

        builder = openassets.transactions.TransactionBuilder(self.configuration.dust_limit)
        colored_outputs = yield from self._get_unspent_outputs(from_address)

        issuance_parameters = openassets.transactions.TransferParameters(
            colored_outputs, to_address.to_scriptPubKey(), from_address.to_scriptPubKey(), self._as_int(amount))

        transaction = builder.issue(issuance_parameters, bytes(metadata, encoding='utf-8'), self._get_fees(fees))

        return self.tx_parser((yield from self._process_transaction(transaction, mode)))

    @asyncio.coroutine
    def distribute(self,
        address: "The address to distribute the asset from",
        forward_address: "The address where to forward the collected bitcoin funds",
        price: "Price of an asset unit in satoshis",
        metadata: "The metadata to embed in the transaction"='',
        fees: "The fess in satoshis for the transaction"=None,
        mode: """'broadcast' for signing and broadcasting the transaction,
            'signed' for signing the transaction without broadcasting,
            'unsigned' for getting the raw unsigned transaction without broadcasting,
            'preview' (default) for displaying a preview of the transactions"""='preview'
    ):
        """For every inbound transaction sending bitcoins to 'address', create an outbound transaction sending back
        to the sender newly issued assets, and send the bitcoins to the forward address. The number of issued coins
        sent back is proportional to the number of bitcoins sent, and configurable through the ratio argument.
        Because the asset issuance transaction is chained from the inbound transaction, double spend is impossible."""
        from_address = self._as_any_address(address)
        to_address = self._as_any_address(forward_address)
        decimal_price = self._as_decimal(price)
        builder = openassets.transactions.TransactionBuilder(self.configuration.dust_limit)
        colored_outputs = yield from self._get_unspent_outputs(from_address)

        transactions = []
        summary = []
        for output in colored_outputs:
            incoming_transaction = yield from self.provider.get_transaction(output.out_point.hash)
            script = bytes(incoming_transaction.vout[0].scriptPubKey)
            collected, amount_issued, change = self._calculate_distribution(
                output.output.value, decimal_price, self._get_fees(fees), self.configuration.dust_limit)

            if amount_issued > 0:
                inputs = [bitcoin.core.CTxIn(output.out_point, output.output.script)]
                outputs = [
                    builder._get_colored_output(script),
                    builder._get_marker_output([amount_issued], bytes(metadata, encoding='utf-8')),
                    builder._get_uncolored_output(to_address.to_scriptPubKey(), collected)
                ]

                if change > 0:
                    outputs.append(builder._get_uncolored_output(script, change))

                transaction = bitcoin.core.CTransaction(vin=inputs, vout=outputs)

                transactions.append(transaction)
                summary.append({
                    'from': self.convert.script_to_display_string(script),
                    'received': self.convert.to_coin(output.output.value) + " BTC",
                    'collected': self.convert.to_coin(collected) + " BTC",
                    'sent': str(amount_issued) + " Units",
                    'transaction': bitcoin.core.b2lx(output.out_point.hash)
                })

        if mode == 'preview':
            return summary
        else:
            result = []
            for transaction in transactions:
                result.append(self.tx_parser((yield from self._process_transaction(transaction, mode))))

            return result

    @staticmethod
    def _calculate_distribution(output_value, price, fees, dust_limit):
        effective_amount = output_value - fees - dust_limit
        units_issued = int(effective_amount / price)

        collected = int(math.ceil(units_issued * price))
        change = effective_amount - collected
        if change < dust_limit:
            collected += change

        return collected, units_issued, effective_amount - collected

    # Private methods

    @staticmethod
    def _as_any_address(address):
        try:
            result = colorcore.addresses.Base58Address.from_string(address)
            return result.address
        except (bitcoin.wallet.CBitcoinAddressError, bitcoin.base58.Base58ChecksumError):
            raise colorcore.routing.ControllerError("The address {} is an invalid address.".format(address))

    def _as_openassets_address(self, address):
        try:
            result = colorcore.addresses.Base58Address.from_string(address)
            if result.namespace != self.configuration.namespace:
                raise colorcore.routing.ControllerError("The address {} is not an asset address.".format(address))
            return result.address
        except (bitcoin.wallet.CBitcoinAddressError, bitcoin.base58.Base58ChecksumError):
            raise colorcore.routing.ControllerError("The address {} is an invalid address.".format(address))

    @staticmethod
    def _as_int(value):
        try:
            return int(value)
        except ValueError:
            raise colorcore.routing.ControllerError("Value '{}' is not a valid integer.".format(value))

    @staticmethod
    def _as_decimal(value):
        try:
            return decimal.Decimal(value)
        except decimal.InvalidOperation:
            raise colorcore.routing.ControllerError("Value '{}' is not a valid decimal number.".format(value))

    def _get_fees(self, value):
        if value is None:
            return self.configuration.default_fees
        else:
            return self._as_int(value)

    @asyncio.coroutine
    def _get_unspent_outputs(self, address, **kwargs):
        cache = self.cache_factory()
        engine = openassets.protocol.ColoringEngine(self.provider.get_transaction, cache, self.event_loop)

        # unspent = yield from self.provider.list_unspent(None if address is None else [str(address)], **kwargs)
        unspent = self._get_utxos_from_chainpi(str(address))

        print('unspent', unspent)

        result = []
        for item in unspent:
            output_result = yield from engine.get_output(item['outpoint'].hash, item['outpoint'].n)
            output = openassets.transactions.SpendableOutput(
                bitcoin.core.COutPoint(item['outpoint'].hash, item['outpoint'].n), output_result)
            output.confirmations = item['confirmations']
            result.append(output)

        # Commit new outputs to cache
        yield from cache.commit()
        print('result', result)
        return result

    @asyncio.coroutine
    def _process_transaction(self, transaction, mode):
        if mode == 'broadcast' or mode == 'signed':
            # Sign the transaction
            signed_transaction = yield from self.provider.sign_transaction(transaction)
            if not signed_transaction['complete']:
                raise colorcore.routing.ControllerError("Could not sign the transaction.")

            if mode == 'broadcast':
                result = yield from self.provider.send_transaction(signed_transaction['tx'])
                return bitcoin.core.b2lx(result)
            else:
                return signed_transaction['tx']
        else:
            # Return the transaction in raw format as a hex string
            return transaction

    #FLUENT CODE
    def _get_utxos_from_chainpi(self, address, **kwargs):
        connection = httplib.HTTPConnection('localhost:3000')
        headers = {'Content-type': 'application/json'}
        endpoint = '/api/addrs/' + address +'?unspentOnly=true'
        connection.request('GET', endpoint)
        response = connection.getresponse()
        data = json.loads(response.read().decode())
        utxos = self._format_utxos(data['txrefs'], address)
        return utxos

    def _format_utxos(self, utxos, address, **kwargs):
        formattedUTXOs = []
        for utxo in utxos:
            utxo['hash'] = utxo['tx_hash']
            utxo['n'] = utxo['tx_output_n']
            utxo['vout'] = self._get_utxo_vout(utxo, address)
            utxo['outpoint'] = COutPoint(lx(utxo['hash']))
            utxo['address'] = CBitcoinAddress(address)
            utxo['scriptPubKey'] = CScript(binascii.unhexlify(utxo['script']))
            utxo['value'] = int(utxo['value'])
        return utxos

    def _get_utxo_vout(self, utxo, address, **kwargs):
        scriptPubKey = {}
        scriptPubKey['hex'] = utxo['script']
        scriptPubKey['addresses'] = [address]

        vout = {}
        vout['value'] = utxo['value']
        vout['n'] = utxo['n']
        vout['scriptPubKey'] = scriptPubKey
        print(vout)
        return vout

    def lx(h):
        """Convert a little-endian hex string to bytes

        Lets you write uint256's and uint160's the way the Satoshi codebase shows
        them.
        """
        import sys
        if sys.version > '3':
            return binascii.unhexlify(h.encode('utf8'))[::-1]
        else:
            return binascii.unhexlify(h)[::-1]

class Convert(object):
    """Provides conversion helpers."""

    def __init__(self, asset_byte):
        self.asset_byte = asset_byte

    @staticmethod
    def to_coin(satoshis):
        return '{0:.8f}'.format(decimal.Decimal(satoshis) / decimal.Decimal(bitcoin.core.COIN))

    def base58_to_asset_id(self, base58_asset_id):
        """
        Parses a base58 asset ID into its bytes representation.

        :param str base58_asset_id: The asset ID in base 58 representation.
        :return: The byte representation of the asset ID.
        :rtype: bytes
        """
        try:
            asset_id = bitcoin.base58.CBase58Data(base58_asset_id)
        except bitcoin.base58.Base58ChecksumError:
            raise colorcore.routing.ControllerError("Invalid asset ID.")

        if asset_id.nVersion != self.asset_byte or len(asset_id) != 20:
            raise colorcore.routing.ControllerError("Invalid asset ID.")

        return bytes(asset_id)

    def asset_id_to_base58(self, asset_id):
        """
        Returns the base 58 representation of an asset ID.

        :param bytes asset_id: The asset ID.
        :return: The base58 representation of the asset ID.
        :rtype: str
        """
        return str(bitcoin.base58.CBase58Data.from_bytes(asset_id, self.asset_byte))

    @staticmethod
    def script_to_address(script):
        """
        Converts an output script to an address if possible, or None otherwise.

        :param bytes script: The script to convert.
        :return: The converted value.
        :rtype: CBitcoinAddress | None
        """
        try:
            return bitcoin.wallet.CBitcoinAddress.from_scriptPubKey(bitcoin.core.CScript(script))
        except bitcoin.wallet.CBitcoinAddressError:
            return None

    @classmethod
    def script_to_display_string(cls, script):
        """
        Converts an output script to an address if possible, or a fallback string otherwise.

        :param bytes script: The script to convert
        :return: The converted value.
        :rtype: str
        """
        address = cls.script_to_address(script)
        return str(address) if address is not None else "Unknown script"

