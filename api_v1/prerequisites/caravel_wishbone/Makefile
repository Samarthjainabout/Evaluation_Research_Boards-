TOOLCHAIN_PATH ?= /usr/bin/
TOOLCHAIN_PREFIX ?= riscv64-unknown-elf
ARCH ?= rv32i_zicsr
PATTERN = gui_wb_mode
WB_OPERATION ?= read
WB_WRITE_VALUE ?= 0x500888FF
WB_UART_TAG ?= 0x52

ifeq ($(WB_OPERATION),write)
WB_DEFINES = -DWB_OPERATION_WRITE=1 -DWB_WRITE_VALUE=$(WB_WRITE_VALUE) -DWB_UART_TAG_WRITE=$(WB_UART_TAG)
else
WB_DEFINES = -DWB_OPERATION_WRITE=0 -DWB_WRITE_VALUE=$(WB_WRITE_VALUE) -DWB_UART_TAG_READ=$(WB_UART_TAG)
endif

.SUFFIXES:

hex: $(PATTERN).hex

$(PATTERN).elf: $(PATTERN).c ../../sections.lds ../../crt0_vex.S
	$(TOOLCHAIN_PATH)$(TOOLCHAIN_PREFIX)-gcc -I../../ -I../../generated/ -O0 \
		-mabi=ilp32 -march=$(ARCH) -D__vexriscv__ $(WB_DEFINES) \
		-Wl,-Bstatic,-T,../../sections.lds,--strip-debug -ffreestanding -nostdlib \
		-o $@ ../../crt0_vex.S ../../isr.c ../../stub.c $<
	$(TOOLCHAIN_PATH)$(TOOLCHAIN_PREFIX)-objdump -D $@ > $(PATTERN).lst

$(PATTERN).hex: $(PATTERN).elf
	$(TOOLCHAIN_PATH)$(TOOLCHAIN_PREFIX)-objcopy -O verilog $< $@
	sed -i 's/@1000/@0000/g' $@

clean:
	rm -f $(PATTERN).elf $(PATTERN).hex $(PATTERN).bin $(PATTERN).lst

.PHONY: clean hex
