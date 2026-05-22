from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from monitoring.models import Infrastructuur, Meetparameter, Meting, Net, Sensor


class SensorImportViewTests(TestCase):
    @patch("monitoring.views.requests.get")
    def test_import_maakt_meerdere_meting_types_en_voorkomt_duplicaten(self, mock_get):
        response_mock = MagicMock()
        response_mock.status_code = 200
        response_mock.json.return_value = {
            "results": [
                {
                    "eancode": "EAN-001",
                    "voltagelevel": 150,
                    "dso": "DSO-A",
                    "station": "Station A",
                    "location": "Locatie A",
                    "region": "Regio A",
                    "datetime": "2026-05-22T10:00:00+00:00",
                    "infeedvalue": 12.34,
                    "actualfrequency": 49.95,
                    "qualitystatus": "in_spec",
                }
            ]
        }
        mock_get.return_value.__enter__.return_value = response_mock
        mock_get.return_value.__exit__.return_value = False

        import_url = reverse("monitoring:importeer_sensoren")

        eerste = self.client.get(import_url)
        self.assertEqual(eerste.status_code, 302)
        self.assertEqual(Meting.objects.count(), 2)

        sensor = Sensor.objects.get(sensor_id="EAN-001")
        self.assertEqual(sensor.metingen.count(), 2)
        self.assertSetEqual(
            set(sensor.metingen.values_list("parameter__naam", flat=True)),
            {"infeedvalue", "frequentie"},
        )

        tweede = self.client.get(import_url)
        self.assertEqual(tweede.status_code, 302)
        self.assertEqual(Meting.objects.count(), 2)


class SensorDetailViewTests(TestCase):
    def test_sensor_detail_toont_meerdere_parameters_en_freq_status_alleen_voor_frequentie(self):
        net = Net.objects.create(net_id="NET-1", type="Type", spanningsniveau=150, freq_min=49.5, freq_max=50.5)
        infra = Infrastructuur.objects.create(
            infrastructuur_id="INFRA-1",
            naam="Infra",
            type="Distributiestation",
            locatie="Locatie",
            status="actief",
            beheerder="DSO",
        )
        sensor = Sensor.objects.create(
            sensor_id="SENS-1",
            type="Testsensor",
            net=net,
            infrastructuur=infra,
            communicatie_protocol="N.v.t.",
            status="actief",
        )

        param_freq = Meetparameter.objects.create(
            naam="frequentie", eenheid="Hz", drempel_onder=49.5, drempel_boven=50.5
        )
        param_infeed = Meetparameter.objects.create(
            naam="infeedvalue", eenheid="MW", drempel_onder=-5000, drempel_boven=5000
        )

        freq_meting = Meting.objects.create(
            sensor=sensor, parameter=param_freq, waarde=51.0, kwaliteit="waarschuwing"
        )
        infeed_meting = Meting.objects.create(
            sensor=sensor, parameter=param_infeed, waarde=-20.0, kwaliteit="teruglevering", infeed_value=-20.0
        )
        timestamp = timezone.now()
        Meting.objects.filter(pk=freq_meting.pk).update(tijdstip=timestamp)
        Meting.objects.filter(pk=infeed_meting.pk).update(tijdstip=timestamp)

        response = self.client.get(reverse("monitoring:sensor_detail", args=[sensor.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Parameter")
        self.assertContains(response, "frequentie")
        self.assertContains(response, "infeedvalue")
        self.assertContains(response, 'class="freq-danger"', count=1)
        self.assertContains(response, "N.v.t.")
