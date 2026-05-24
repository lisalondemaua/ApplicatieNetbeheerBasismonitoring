from django.core.management.base import BaseCommand

from monitoring.data_generator import generate_demo_data


class Command(BaseCommand):
    help = "Genereer demo/testdata voor monitoring."

    def add_arguments(self, parser):
        parser.add_argument(
            "--use-api",
            action="store_true",
            help="Gebruik Elia API data als bron (fallback naar synthetische data bij fouten).",
        )

    def handle(self, *args, **options):
        summary = generate_demo_data(use_api=options["use_api"], stdout=self.stdout.write)
        self.stdout.write(self.style.SUCCESS("Datageneratie voltooid."))
        for key, value in summary.items():
            self.stdout.write(f"- {key}: {value}")
