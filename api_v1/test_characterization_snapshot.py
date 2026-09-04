import unittest
from plot_characterization_snapshot import pulse_pairs, binned


class SnapshotTests(unittest.TestCase):
    def read(self,g):
        return {'kind':'preparation_verify','utc':'synthetic',
                'reading':{'current_uA':g*.5,'capture':'synthetic','feedback_attempts':1},
                'result':{'ok':True,'operation':'read','cell':{'row':0,'col':0},'rails':{'vcc_set_v':.5}}}

    def pair(self,before,after,mode='set',include_ack=True,include_negative_g=False):
        events=[self.read(before),{'kind':'pulse_intent'}]
        if include_ack:
            events.append({'kind':'pulse_ack','role':'preparation','utc':'synthetic',
                           'result':{'ok':True,'operation':mode,'cell':{'row':0,'col':0},
                                     'rails':{'vcc_set_v':2.3,'vcc_wl_set_v':1.32}}})
        events.append(self.read(after))
        return pulse_pairs({'id':'t001','status':'running','events':events},
                           {'read_vcc_set_V':.5,'cell':[0,0]},include_negative_g)

    def test_negative_delta_preserved(self):
        pairs,rejected=self.pair(30,20)
        self.assertEqual(pairs[0]['delta_g_uS'],-10)
        self.assertEqual(rejected,[])

    def test_reset_sign(self):
        pairs,_=self.pair(30,20,'reset')
        self.assertEqual(pairs[0]['delta_g_uS'],10)

    def test_negative_conductance_excluded(self):
        for before,after in [(-1,10),(10,-1)]:
            pairs,rejected=self.pair(before,after)
            self.assertEqual(pairs,[])
            self.assertEqual(rejected[0]['reason'],'negative_before_or_after_G')

    def test_negative_conductance_can_be_included(self):
        pairs,rejected=self.pair(-1,10,include_negative_g=True)
        self.assertEqual(rejected,[])
        self.assertEqual(pairs[0]['g_before_uS'],-1)
        self.assertEqual(pairs[0]['delta_g_uS'],11)

    def test_uncertain_pulse_not_included(self):
        pairs,rejected=self.pair(30,20,include_ack=False)
        self.assertEqual(pairs,[])
        self.assertEqual(len(rejected),1)

    def test_repeated_bins_mean_and_count(self):
        a,_=self.pair(30,20)
        b,_=self.pair(30,50)
        bins=binned(a+b)
        self.assertEqual(len(bins),1)
        self.assertEqual(bins[0]['count'],2)
        self.assertEqual(bins[0]['mean_delta_uS'],5)


if __name__=='__main__':
    unittest.main()
