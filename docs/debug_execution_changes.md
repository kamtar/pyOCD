# Debug execution correctness changes

This change addresses seven debugger issues reviewed against commit `9250ca08`.

- Direct core single stepping temporarily restores a managed software breakpoint's
  original instruction, then reinstalls the breakpoint after the core halts. A
  firmware-authored BKPT is not silently skipped. Range stepping stops before a
  subsequent managed breakpoint. GDB-managed removal/reinsertion remains compatible.
- Instruction stepping checks observed S_HALT, reports instruction timeouts, and
  requests and confirms halt on cancellation or transfer failure. Recovery is
  bounded by `reset.halt_timeout`. Interrupt masking is restored when halt can be
  confirmed. A disconnected or inaccessible core cannot be guaranteed recoverable.
- Core memory writes preserve installed software breakpoints and update their
  saved instructions, including partial-byte and block overlaps. Deferred writes
  are completed before committing saved instructions. Raw AP writes deliberately
  bypass this logical view, including writes used by the breakpoint provider.
- Aligned word reads filter software breakpoints at the correct four-byte stride.
- GDB stop replies identify matched read, write, and access watchpoints. DWT match
  status is cached for repeated stop queries and cleared before another run.
  The reported address identifies the watched object, not a reconstructed bus
  transaction address. Only one matching logical watchpoint is reported.
- DWTv2 can cover larger or unaligned ranges using multiple aligned 1-, 2-, and
  4-byte address comparators. Allocation is bounded by available comparators;
  unsupported register modes are rejected by readback and partial installations
  are disabled. Removal releases the entire logical group. This does not add
  hardware linked-range or data-value comparison modes.
- GDB c/s and C/S execution addresses are honored, including address zero. The
  historical semicolon spelling for c/s is still accepted.

## Automated validation

`test/unit/test_debug_execution.py` exercises breakpoint memory integrity,
error recovery, range stepping, watchpoint allocation and rollback, stop replies,
and GDB execution addresses using a simulated memory AP. Run:

```sh
python -m pytest test/unit -q
```

## Hardware validation still required

Before treating this change as hardware-qualified, test a Cortex-M target with
RAM execution, hardware breakpoints, and DWT; additionally test an Armv8-M target
for multi-comparator ranges. Exercise:

1. 16- and 32-bit Thumb instructions at software breakpoints, branches, IT blocks,
   and original BKPT instructions, through both Commander and GDB.
2. Stepping with interrupts enabled and masked, fault entry, range loops, Ctrl-C,
   timeouts, and probe disconnection during an instruction.
3. Byte/halfword/word writes overlapping installed breakpoints, removal afterward,
   and instruction-cache visibility on cached cores.
4. Read/write/access watchpoints, byte and wider accesses throughout a grouped
   range, comparator exhaustion, unsupported modes, and repeated GDB stop queries.
5. Multicore/shared executable memory: these changes do not add coordinated
   cross-core software-breakpoint ownership or atomic step-over across cores.

## Protocol and hardware references

- [GDB execution packets](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Packets.html)
- [GDB stop replies](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Stop-Reply-Packets.html)
- [Arm DWT register description](https://developer.arm.com/documentation/ddi0337/e/ch11s05s01)
- [RP2350 datasheet, Cortex-M33 DWT registers](https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf)
