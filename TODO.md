# Try

  - Benchmark FUSE `passthrough.c` to see if I match 4K randreads at 5MB/s
    https://lwn.net/Articles/843873/

  - Maybe not worth it: prod version could park FDs for recovery in
    with `systemd`, instead of relying on paths.


# Read

  - `io_uring` [docs](https://unixism.net/loti/) and
    [paper](https://kernel.dk/io_uring.pdf).

  - `ublk` [docs](https://www.kernel.org/doc/html/latest/block/ublk.html)
    and [intro](https://github.com/ming1/ubdsrv/blob/master/doc/ublk_intro.pdf).

  - `ublk` [recovery](https://lwn.net/Articles/906097/). Plus comments:

      * See if "Sartura" Rust ublk now exists. 
      * Read about [DADI](
        https://www.usenix.org/system/files/atc20-li-huiba.pdf).
      * Investigate Nydus + EROFS.
      * See if "nbdublk" has any interesting notes or benchmarks.
   


