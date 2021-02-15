#!/usr/bin/python
# vim: tabstop=4 expandtab shiftwidth=4

import argparse
import requests
import re
import time
import threading
from datetime import datetime
from os import environ

# Prometheus client library
from prometheus_client import CollectorRegistry
from prometheus_client.core import Gauge, Counter
from prometheus_client.exposition import CONTENT_TYPE_LATEST, generate_latest


try:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn
except ImportError:
    # Python 3
    unicode = str
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn


parser = argparse.ArgumentParser(description='simple digitalbits-core Prometheus exporter/scraper')
parser.add_argument('--digitalbits-core-address', type=str,
                    help='DigitalBits core address. Defaults to DIGITALBITS_CORE_ADDRESS environment '
                         'variable or if not set to http://127.0.0.1:11626',
                    default=environ.get('DIGITALBITS_CORE_ADDRESS', 'http://127.0.0.1:11626'))
parser.add_argument('--port', type=int,
                    help='HTTP bind port. Defaults to PORT environment variable '
                         'or if not set to 9473',
                    default=int(environ.get('PORT', '9473')))
args = parser.parse_args()


class _ThreadingSimpleServer(ThreadingMixIn, HTTPServer):
    """Thread per request HTTP server."""
    # Copied from prometheus client_python
    daemon_threads = True


class DigitalBitsCoreHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def get_labels(self):
        try:
            response = requests.get(self.info_url)
            json = response.json()
            build = json['info']['build']
            network = json['info']['network']
        except Exception:
            return ['unknown', 'unknown', 'unknown', 'unknown', 'unknown']
        match = self.build_regex.match(build)
        build = re.sub('\s', '_', build).lower()
        build = re.sub('\(|\)', '', build)

        if not match:
            return ['unknown', 'unknown', 'unknown', build, network]

        labels = [
            match.group(2),
            match.group(3),
            match.group(4),
            build,
            network,
        ]
        return labels

    def duration_to_seconds(self, duration, duration_unit):
        # given duration and duration_unit, returns duration in seconds
        time_units_to_seconds = {
            'd':  'duration * 86400.0',
            'h':  'duration * 3600.0',
            'm':  'duration * 60.0',
            's':  'duration / 1.0',
            'ms': 'duration / 1000.0',
            'us': 'duration / 1000000.0',
            'ns': 'duration / 1000000000.0',
        }
        return eval(time_units_to_seconds[duration_unit])

    def buckets_to_metrics(self, metric_name, buckets):
        # Converts raw bucket metric into sorted list of buckets
        unit = buckets['boundary_unit']
        description = 'libmedida metric type: ' + buckets['type']
        c = Counter(metric_name + '_count', description, self.label_names, registry=self.registry)
        s = Counter(metric_name + '_sum', description, self.label_names, registry=self.registry)
        g = Gauge(metric_name + '_bucket', description, self.label_names + ['le'], registry=self.registry)

        measurements = []
        for bucket in buckets['buckets']:
            measurements.append({
                'boundary': self.duration_to_seconds(bucket['boundary'], unit),
                'count': bucket['count'],
                'sum': bucket['sum']
                }
            )
        count = 0
        for m in sorted(measurements, key=lambda i: i['boundary']):
            # Buckets from core contain only values from their respective ranges.
            # Prometheus expects "le" buckets to be cummulative so we need some extra math
            count += m['count']
            c.labels(*self.labels).inc(m['count'])
            s.labels(*self.labels).inc(self.duration_to_seconds(m['sum'], unit))
            # Treat buckets larger than 30d as infinity
            if float(m['boundary']) > 30 * 86400:
                g.labels(*self.labels + ['+Inf']).inc(count)
            else:
                g.labels(*self.labels + [m['boundary']]).inc(count)

    def set_vars(self):
        self.info_url = args.digitalbits_core_address + '/info'
        self.metrics_url = args.digitalbits_core_address + '/metrics'
        self.cursors_url = args.digitalbits_core_address + '/getcursor'
        self.info_keys = ['ledger', 'network', 'peers', 'protocol_version', 'quorum', 'startedOn', 'state']
        self.state_metrics = ['booting', 'joining scp', 'connected', 'catching up', 'synced', 'stopping']
        self.ledger_metrics = {'age': 'age', 'baseFee': 'base_fee', 'baseReserve': 'base_reserve',
                               'closeTime': 'close_time', 'maxTxSetSize': 'max_tx_set_size',
                               'num': 'num', 'version': 'version'}
        self.quorum_metrics = ['agree', 'delayed', 'disagree', 'fail_at', 'missing']
        self.quorum_phase_metrics = ['unknown', 'prepare', 'confirm', 'externalize']
        # Examples:
        #   "digitalbits-core 11.1.0-unstablerc2 (324c1bd61b0e9bada63e0d696d799421b00a7950)"
        #   "digitalbits-core 11.1.0 (324c1bd61b0e9bada63e0d696d799421b00a7950)"
        #   "v11.1.0"
        self.build_regex = re.compile('(digitalbits-core|v) ?(\d+)\.(\d+)\.(\d+).*$')

        self.registry = CollectorRegistry()
        self.label_names = ["ver_major", "ver_minor", "ver_patch", "build", "network"]
        self.labels = self.get_labels()

    def error(self, code, msg):
        self.send_response(code)
        self.send_header('Content-Type', CONTENT_TYPE_LATEST)
        self.end_headers()
        self.wfile.write('{}\n'.format(msg).encode('utf-8'))

    def do_GET(self):
        self.set_vars()
        ###########################################
        # Export metrics from the /metrics endpoint
        ###########################################
        try:
            response = requests.get(self.metrics_url)
        except requests.ConnectionError:
            self.error(504, 'Error retrieving data from {}'.format(self.metrics_url))
            return
        if not response.ok:
            self.error(504, 'Error retrieving data from {}'.format(self.metrics_url))
            return
        try:
            metrics = response.json()['metrics']
        except ValueError:
            self.error(500, 'Error parsing metrics JSON data')
            return
        # iterate over all metrics
        for k in metrics:
            metric_name = re.sub('\.|-|\s', '_', k).lower()
            metric_name = 'digitalbits_core_' + metric_name

            if metrics[k]['type'] == 'timer':
                # we have a timer, expose as a Prometheus Summary
                # we convert digitalbits-core time units to seconds, as per Prometheus best practices
                metric_name = metric_name + '_seconds'
                if 'sum' in metrics[k]:
                    # use libmedida sum value
                    total_duration = metrics[k]['sum']
                else:
                    # compute sum value
                    total_duration = (metrics[k]['mean'] * metrics[k]['count'])
                c = Counter(metric_name + '_count', 'libmedida metric type: ' + metrics[k]['type'],
                            self.label_names, registry=self.registry)
                c.labels(*self.labels).inc(metrics[k]['count'])
                s = Counter(metric_name + '_sum', 'libmedida metric type: ' + metrics[k]['type'],
                            self.label_names, registry=self.registry)
                s.labels(*self.labels).inc(self.duration_to_seconds(total_duration, metrics[k]['duration_unit']))

                # add digitalbits-core calculated quantiles to our summary
                summary = Gauge(metric_name, 'libmedida metric type: ' + metrics[k]['type'],
                                self.label_names + ['quantile'], registry=self.registry)
                summary.labels(*self.labels + ['0.75']).set(
                    self.duration_to_seconds(metrics[k]['75%'], metrics[k]['duration_unit']))
                summary.labels(*self.labels + ['0.99']).set(
                    self.duration_to_seconds(metrics[k]['99%'], metrics[k]['duration_unit']))
            elif metrics[k]['type'] == 'histogram':
                if 'count' not in metrics[k]:
                    # DigitalBits-core version too old, we don't have required data
                    continue
                c = Counter(metric_name + '_count', 'libmedida metric type: ' + metrics[k]['type'],
                            self.label_names, registry=self.registry)
                c.labels(*self.labels).inc(metrics[k]['count'])
                s = Counter(metric_name + '_sum', 'libmedida metric type: ' + metrics[k]['type'],
                            self.label_names, registry=self.registry)
                s.labels(*self.labels).inc(metrics[k]['sum'])

                # add digitalbits-core calculated quantiles to our summary
                summary = Gauge(metric_name, 'libmedida metric type: ' + metrics[k]['type'],
                                self.label_names + ['quantile'], registry=self.registry)
                summary.labels(*self.labels + ['0.75']).set(metrics[k]['75%'])
                summary.labels(*self.labels + ['0.99']).set(metrics[k]['99%'])
            elif metrics[k]['type'] == 'counter':
                # we have a counter, this is a Prometheus Gauge
                g = Gauge(metric_name, 'libmedida metric type: ' + metrics[k]['type'], self.label_names, registry=self.registry)
                g.labels(*self.labels).set(metrics[k]['count'])
            elif metrics[k]['type'] == 'meter':
                # we have a meter, this is a Prometheus Counter
                c = Counter(metric_name, 'libmedida metric type: ' + metrics[k]['type'], self.label_names, registry=self.registry)
                c.labels(*self.labels).inc(metrics[k]['count'])
            elif metrics[k]['type'] == 'buckets':
                # We have a bucket, this is a Prometheus Histogram
                self.buckets_to_metrics(metric_name, metrics[k])

        #######################################
        # Export metrics from the info endpoint
        #######################################
        try:
            response = requests.get(self.info_url)
        except requests.ConnectionError:
            self.error(504, 'Error retrieving data from {}'.format(self.info_url))
            return
        if not response.ok:
            self.error(504, 'Error retrieving data from {}'.format(self.info_url))
            return
        try:
            info = response.json()['info']
        except ValueError:
            self.error(500, 'Error parsing info JSON data')
            return
        if not all([i in info for i in self.info_keys]):
            self.error(500, 'Error - info endpoint did not return all required fields')
            return

        # Ledger metrics
        for core_name, prom_name in self.ledger_metrics.items():
            g = Gauge('digitalbits_core_ledger_{}'.format(prom_name),
                      'DigitalBits core ledger metric name: {}'.format(core_name),
                      self.label_names, registry=self.registry)
            g.labels(*self.labels).set(info['ledger'][core_name])

        # Version 11.2.0 and later report quorum metrics in the following format:
        # "quorum" : {
        #    "qset" : {
        #      "agree": 3
        #
        # Older versions use this format:
        # "quorum" : {
        #   "758110" : {
        #     "agree" : 3,
        if 'qset' in info['quorum']:
            tmp = info['quorum']['qset']
        else:
            tmp = info['quorum'].values()[0]
        if not tmp:
            self.error(500, 'Error - missing quorum data')
            return

        for metric in self.quorum_metrics:
            g = Gauge('digitalbits_core_quorum_{}'.format(metric),
                      'DigitalBits core quorum metric: {}'.format(metric),
                      self.label_names, registry=self.registry)
            g.labels(*self.labels).set(tmp[metric])

        for metric in self.quorum_phase_metrics:
            g = Gauge('digitalbits_core_quorum_phase_{}'.format(metric),
                      'DigitalBits core quorum phase {}'.format(metric),
                      self.label_names, registry=self.registry)
            if tmp['phase'].lower() == metric:
                g.labels(*self.labels).set(1)
            else:
                g.labels(*self.labels).set(0)

        # Versions >=11.2.0 expose more info about quorum
        if 'transitive' in info['quorum']:
            g = Gauge('digitalbits_core_quorum_transitive_intersection',
                      'DigitalBits core quorum transitive intersection',
                      self.label_names, registry=self.registry)
            if info['quorum']['transitive']['intersection']:
                g.labels(*self.labels).set(1)
            else:
                g.labels(*self.labels).set(0)
            g = Gauge('digitalbits_core_quorum_transitive_last_check_ledger',
                      'DigitalBits core quorum transitive last_check_ledger',
                      self.label_names, registry=self.registry)
            g.labels(*self.labels).set(info['quorum']['transitive']['last_check_ledger'])
            g = Gauge('digitalbits_core_quorum_transitive_node_count',
                      'DigitalBits core quorum transitive node_count',
                      self.label_names, registry=self.registry)
            g.labels(*self.labels).set(info['quorum']['transitive']['node_count'])
            # Versions >=11.3.0 expose "critical" key
            if 'critical' in info['quorum']['transitive']:
                g = Gauge('digitalbits_core_quorum_transitive_critical',
                          'DigitalBits core quorum transitive critical',
                          self.label_names + ['critical_validators'], registry=self.registry)
                if info['quorum']['transitive']['critical']:
                    for peer_list in info['quorum']['transitive']['critical']:
                        critical_peers = ','.join(sorted(peer_list))  # label value is comma separated listof peers
                        l = self.labels + [critical_peers]
                        g.labels(*l).set(1)
                else:
                    l = self.labels + ['null']
                    g.labels(*l).set(0)

        # Peers metrics
        g = Gauge('digitalbits_core_peers_authenticated_count',
                  'DigitalBits core authenticated_count count',
                  self.label_names, registry=self.registry)
        g.labels(*self.labels).set(info['peers']['authenticated_count'])
        g = Gauge('digitalbits_core_peers_pending_count',
                  'DigitalBits core pending_count count',
                  self.label_names, registry=self.registry)
        g.labels(*self.labels).set(info['peers']['pending_count'])

        g = Gauge('digitalbits_core_protocol_version',
                  'DigitalBits core protocol_version',
                  self.label_names, registry=self.registry)
        g.labels(*self.labels).set(info['protocol_version'])

        for metric in self.state_metrics:
            name = re.sub('\s', '_', metric)
            g = Gauge('digitalbits_core_{}'.format(name),
                      'DigitalBits core state {}'.format(metric),
                      self.label_names, registry=self.registry)
            if info['state'].lower().startswith(metric):  # Use startswith to work around "!"
                g.labels(*self.labels).set(1)
            else:
                g.labels(*self.labels).set(0)

        g = Gauge('digitalbits_core_started_on', 'DigitalBits core start time in epoch', self.label_names, registry=self.registry)
        date = datetime.strptime(info['startedOn'], "%Y-%m-%dT%H:%M:%SZ")
        g.labels(*self.labels).set(int(date.strftime('%s')))

        #######################################
        # Export cursor metrics
        #######################################
        try:
            response = requests.get(self.cursors_url)
        except requests.ConnectionError:
            self.error(504, 'Error retrieving data from {}'.format(self.cursors_url))
            return

        # Some server modes we want to scrape do not support 'getcursors' command at all.
        # These just respond with a 404 and the non-json informative unknown-commands output.
        if not response.ok and response.status_code != 404:
            self.error(504, 'Error retrieving data from {}'.format(self.cursors_url))
            return

        if "Supported HTTP commands" not in str(response.content):
            try:
                cursors = response.json()['cursors']
            except ValueError:
                self.error(500, 'Error parsing cursor JSON data')
                return

            g = Gauge('digitalbits_core_active_cursors',
                      'DigitalBits core active cursors',
                      self.label_names + ['cursor_name'], registry=self.registry)
            for cursor in cursors:
                if not cursor:
                    continue
                l = self.labels + [cursor.get('id').strip()]
                g.labels(*l).set(cursor['cursor'])

        #######################################
        # Render output
        #######################################
        output = generate_latest(self.registry)
        if not output:
            self.error(500, 'Error - no metrics were genereated')
            return
        self.send_response(200)
        self.send_header('Content-Type', CONTENT_TYPE_LATEST)
        self.end_headers()
        self.wfile.write(output)


def main():
    httpd = _ThreadingSimpleServer(("", args.port), DigitalBitsCoreHandler)
    t = threading.Thread(target=httpd.serve_forever)
    t.daemon = True
    t.start()
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
