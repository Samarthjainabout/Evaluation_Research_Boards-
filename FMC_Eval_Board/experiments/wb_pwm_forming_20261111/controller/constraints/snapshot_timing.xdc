# Source holds the entire packet for 100 ns while a toggle crosses three
# synchronizer stages. Bound intra-packet skew well below that settling time.
# Existing asynchronous clock-group constraints remain unchanged.
set_bus_skew 2.000 \
    -from [get_cells -hier -filter {NAME =~ word_snapshot/held_words_reg*}] \
    -to [get_cells -hier -filter {NAME =~ word_snapshot/destination_words_reg*}]
