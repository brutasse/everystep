from django.core.management.base import BaseCommand

from everystep.worker import Worker


class Command(BaseCommand):
    help = "Run an everystep runner: claim and execute due workflows."

    def add_arguments(self, parser):
        parser.add_argument("--pool", type=int, default=4, help="thread pool size")
        parser.add_argument("--poll", type=float, default=0.2, help="seconds between claim polls")
        parser.add_argument(
            "--drain",
            type=float,
            default=30,
            help=(
                "seconds to wait for in-flight workflows after SIGTERM/SIGINT, at step "
                "boundaries, before requeueing them and exiting (0: wait indefinitely)"
            ),
        )
        parser.add_argument(
            "--name",
            default=None,
            help=(
                "runner name (default: hostname). Must be unique among concurrently "
                "running runners; keep it identical across restarts so a restart "
                "reclaims the runs a crash left behind (a clean shutdown requeues "
                "them, so it needs no name)."
            ),
        )
        parser.add_argument(
            "--metrics-port",
            type=int,
            default=0,
            help=(
                "serve Prometheus metrics on this TCP port (0: disabled). Requires "
                'the metrics extra: pip install "everystep[metrics]".'
            ),
        )
        parser.add_argument(
            "--metrics-bind",
            default="0.0.0.0",
            help="interface to bind the metrics endpoint to (default: 0.0.0.0)",
        )

    def handle(self, *args, **options):
        Worker(
            pool_size=options["pool"],
            poll=options["poll"],
            drain=options["drain"],
            name=options["name"],
            metrics_port=options["metrics_port"],
            metrics_bind=options["metrics_bind"],
        ).run()
