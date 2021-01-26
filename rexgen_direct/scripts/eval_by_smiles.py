import rdkit
from rdkit import Chem
from optparse import OptionParser
from rdkit import RDLogger
import time

lg = RDLogger.logger()
lg.setLevel(4)

'''
This script evaluates the quality of predictions from the rank_diff_wln model by applying the predicted
graph edits to the reactants, cleaning up the generated product, and comparing it to what was recorded
as the true (major) product of that reaction
'''

# Define some post-sanitization reaction cleaning scripts
# These are to align our graph edit representation of a reaction with the data for improved coverage
from rdkit.Chem import AllChem
clean_rxns_presani = [
    AllChem.ReactionFromSmarts('[O:1]=[c:2][n;H0:3]>>[O:1]=[c:2][n;H1:3]'),  # hydroxypyridine written with carbonyl, must invent H on nitrogen
]
clean_rxns_postsani = [
    AllChem.ReactionFromSmarts('[n;H1;+0:1]:[n;H0;+1:2]>>[n;H0;+0:1]:[n;H0;+0:2]'),  #  two adjacent aromatic nitrogens should allow for H shift
    AllChem.ReactionFromSmarts('[n;H1;+0:1]:[c:3]:[n;H0;+1:2]>>[n;H0;+0:1]:[*:3]:[n;H0;+0:2]'),  # two aromatic nitrogens separated by one should allow for H shift
    AllChem.ReactionFromSmarts('[#7;H0;+:1]-[O;H1;+0:2]>>[#7;H0;+:1]-[O;H0;-:2]'),
    AllChem.ReactionFromSmarts('[C;H0;+0:1](=[O;H0;+0:2])[O;H0;-1:3]>>[C;H0;+0:1](=[O;H0;+0:2])[O;H1;+0:3]'),  # neutralize C(=O)[O-]
    AllChem.ReactionFromSmarts('[I,Br,F;H1;D0;+0:1]>>[*;H0;-1:1]'),  # turn neutral halogens into anions EXCEPT HCl
    AllChem.ReactionFromSmarts('[N;H0;-1:1]([C:2])[C:3]>>[N;H1;+0:1]([*:2])[*:3]'),  # inexplicable nitrogen anion in reactants gets fixed in prods
]
for clean_rxn in clean_rxns_presani + clean_rxns_postsani:
    if clean_rxn.Validate() != (0, 0):
        raise ValueError('Invalid cleaning reaction - check your SMARTS!')
BOND_TYPE = [0, Chem.rdchem.BondType.SINGLE, Chem.rdchem.BondType.DOUBLE, Chem.rdchem.BondType.TRIPLE, Chem.rdchem.BondType.AROMATIC]

def edit_mol(rmol, edits):
    new_mol = Chem.RWMol(rmol)

    # Keep track of aromatic nitrogens, might cause explicit hydrogen issues
    aromatic_nitrogen_idx = set()
    aromatic_carbonyl_adj_to_aromatic_nH = {}
    aromatic_carbondeg3_adj_to_aromatic_nH0 = {}
    for a in new_mol.GetAtoms():
        if a.GetIsAromatic() and a.GetSymbol() == 'N':
            aromatic_nitrogen_idx.add(a.GetIdx())
            for nbr in a.GetNeighbors():
                if a.GetNumExplicitHs() == 1 and nbr.GetSymbol() == 'C' and nbr.GetIsAromatic() and any(b.GetBondTypeAsDouble() == 2 for b in nbr.GetBonds()):
                    aromatic_carbonyl_adj_to_aromatic_nH[nbr.GetIdx()] = a.GetIdx()
                elif a.GetNumExplicitHs() == 0 and nbr.GetSymbol() == 'C' and nbr.GetIsAromatic() and len(nbr.GetBonds()) == 3:
                    aromatic_carbondeg3_adj_to_aromatic_nH0[nbr.GetIdx()] = a.GetIdx()
        else:
            a.SetNumExplicitHs(0)
    new_mol.UpdatePropertyCache()

    amap = {}
    for atom in rmol.GetAtoms():
        amap[atom.GetIntProp('molAtomMapNumber')] = atom.GetIdx()

    # Apply the edits as predicted
    for x, y, t in edits:
        bond = new_mol.GetBondBetweenAtoms(amap[x],amap[y])
        a1 = new_mol.GetAtomWithIdx(amap[x])
        a2 = new_mol.GetAtomWithIdx(amap[y])
        if bond is not None:
            new_mol.RemoveBond(amap[x],amap[y])

            # Are we losing a bond on an aromatic nitrogen?
            if bond.GetBondTypeAsDouble() == 1.0:
                if amap[x] in aromatic_nitrogen_idx:
                    if a1.GetTotalNumHs() == 0:
                        a1.SetNumExplicitHs(1)
                    elif a1.GetFormalCharge() == 1:
                        a1.SetFormalCharge(0)
                elif amap[y] in aromatic_nitrogen_idx:
                    if a2.GetTotalNumHs() == 0:
                        a2.SetNumExplicitHs(1)
                    elif a2.GetFormalCharge() == 1:
                        a2.SetFormalCharge(0)

            # Are we losing a c=O bond on an aromatic ring? If so, remove H from adjacent nH if appropriate
            if bond.GetBondTypeAsDouble() == 2.0:
                if amap[x] in aromatic_carbonyl_adj_to_aromatic_nH:
                    new_mol.GetAtomWithIdx(aromatic_carbonyl_adj_to_aromatic_nH[amap[x]]).SetNumExplicitHs(0)
                elif amap[y] in aromatic_carbonyl_adj_to_aromatic_nH:
                    new_mol.GetAtomWithIdx(aromatic_carbonyl_adj_to_aromatic_nH[amap[y]]).SetNumExplicitHs(0)

        if t > 0:
            new_mol.AddBond(amap[x],amap[y],BOND_TYPE[t])

            # Special alkylation case?
            if t == 1:
                if amap[x] in aromatic_nitrogen_idx:
                    if a1.GetTotalNumHs() == 1:
                        a1.SetNumExplicitHs(0)
                    else:
                        a1.SetFormalCharge(1)
                elif amap[y] in aromatic_nitrogen_idx:
                    if a2.GetTotalNumHs() == 1:
                        a2.SetNumExplicitHs(0)
                    else:
                        a2.SetFormalCharge(1)

            # Are we getting a c=O bond on an aromatic ring? If so, add H to adjacent nH0 if appropriate
            if t == 2:
                if amap[x] in aromatic_carbondeg3_adj_to_aromatic_nH0:
                    new_mol.GetAtomWithIdx(aromatic_carbondeg3_adj_to_aromatic_nH0[amap[x]]).SetNumExplicitHs(1)
                elif amap[y] in aromatic_carbondeg3_adj_to_aromatic_nH0:
                    new_mol.GetAtomWithIdx(aromatic_carbondeg3_adj_to_aromatic_nH0[amap[y]]).SetNumExplicitHs(1)

    pred_mol = new_mol.GetMol()

    # Clear formal charges to make molecules valid
    # Note: because S and P (among others) can change valence, be more flexible
    for atom in pred_mol.GetAtoms():
        atom.ClearProp('molAtomMapNumber')
        if atom.GetSymbol() == 'N' and atom.GetFormalCharge() == 1: # exclude negatively-charged azide
            bond_vals = sum([bond.GetBondTypeAsDouble() for bond in atom.GetBonds()])
            if bond_vals <= 3:
                atom.SetFormalCharge(0)
        elif atom.GetSymbol() == 'N' and atom.GetFormalCharge() == -1: # handle negatively-charged azide addition
            bond_vals = sum([bond.GetBondTypeAsDouble() for bond in atom.GetBonds()])
            if bond_vals == 3 and any([nbr.GetSymbol() == 'N' for nbr in atom.GetNeighbors()]):
                atom.SetFormalCharge(0)
        elif atom.GetSymbol() == 'N':
            bond_vals = sum([bond.GetBondTypeAsDouble() for bond in atom.GetBonds()])
            if bond_vals == 4 and not atom.GetIsAromatic(): # and atom.IsInRingSize(5)):
                atom.SetFormalCharge(1)
        elif atom.GetSymbol() == 'C' and atom.GetFormalCharge() != 0:
            atom.SetFormalCharge(0)
        elif atom.GetSymbol() == 'O' and atom.GetFormalCharge() != 0:
            bond_vals = sum([bond.GetBondTypeAsDouble() for bond in atom.GetBonds()]) + atom.GetNumExplicitHs()
            if bond_vals == 2:
                atom.SetFormalCharge(0)
        elif atom.GetSymbol() in ['Cl', 'Br', 'I', 'F'] and atom.GetFormalCharge() != 0:
            bond_vals = sum([bond.GetBondTypeAsDouble() for bond in atom.GetBonds()])
            if bond_vals == 1:
                atom.SetFormalCharge(0)
        elif atom.GetSymbol() == 'S' and atom.GetFormalCharge() != 0:
            bond_vals = sum([bond.GetBondTypeAsDouble() for bond in atom.GetBonds()])
            if bond_vals in [2, 4, 6]:
                atom.SetFormalCharge(0)
        elif atom.GetSymbol() == 'P': # quartenary phosphorous should be pos. charge with 0 H
            bond_vals = [bond.GetBondTypeAsDouble() for bond in atom.GetBonds()]
            if sum(bond_vals) == 4 and len(bond_vals) == 4:
                atom.SetFormalCharge(1)
                atom.SetNumExplicitHs(0)
            elif sum(bond_vals) == 3 and len(bond_vals) == 3: # make sure neutral
                atom.SetFormalCharge(0)
        elif atom.GetSymbol() == 'B': # quartenary boron should be neg. charge with 0 H
            bond_vals = [bond.GetBondTypeAsDouble() for bond in atom.GetBonds()]
            if sum(bond_vals) == 4 and len(bond_vals) == 4:
                atom.SetFormalCharge(-1)
                atom.SetNumExplicitHs(0)
        elif atom.GetSymbol() in ['Mg', 'Zn']:
            bond_vals = [bond.GetBondTypeAsDouble() for bond in atom.GetBonds()]
            if sum(bond_vals) == 1 and len(bond_vals) == 1:
                atom.SetFormalCharge(1)
        elif atom.GetSymbol() == 'Si':
            bond_vals = [bond.GetBondTypeAsDouble() for bond in atom.GetBonds()]
            if sum(bond_vals) == len(bond_vals):
                atom.SetNumExplicitHs(max(0, 4 - len(bond_vals)))

    # Bounce to/from SMILES to try to sanitize
    pred_smiles = Chem.MolToSmiles(pred_mol)
    pred_list = pred_smiles.split('.')
    pred_mols = [Chem.MolFromSmiles(pred_smiles) for pred_smiles in pred_list]

    for i, mol in enumerate(pred_mols):

        # Check if we failed/succeeded in previous step
        if mol is None:
            print('##### Unparseable mol: {}'.format(pred_list[i]))
            continue

        # Else, try post-sanitiztion fixes in structure
        mol = Chem.MolFromSmiles(Chem.MolToSmiles(mol))
        if mol is None:
            continue
        for rxn in clean_rxns_postsani:
            out = rxn.RunReactants((mol,))
            if out:
                try:
                    Chem.SanitizeMol(out[0][0])
                    pred_mols[i] = Chem.MolFromSmiles(Chem.MolToSmiles(out[0][0]))
                except Exception as e:
                    print(e)
                    print('Could not sanitize postsani reaction product: {}'.format(Chem.MolToSmiles(out[0][0])))
                    print('Original molecule was: {}'.format(Chem.MolToSmiles(mol)))
    pred_smiles = [Chem.MolToSmiles(pred_mol) for pred_mol in pred_mols if pred_mol is not None]

    return pred_smiles

if __name__ == "__main__":

    time_start = time.time()

    parser = OptionParser()
    parser.add_option("-t", "--pred", dest="pred_path")   # file containing predicted edits
    parser.add_option("-g", "--gold", dest="gold_path")   # file containing true edits
    parser.add_option("-s", "--singleonly", dest="singleonly", default=False) # only compare single products
    parser.add_option("--bonds_as_doubles", dest="bonds_as_doubles", default=False) # bond types are doubles, not indices
    opts, args = parser.parse_args()

    file_predict = open(opts.pred_path)
    file_gold = open(opts.gold_path)
    file_eval = open(opts.pred_path + '.eval_by_smiles_200714', 'w')

    print('## Bond types in output files are doubles? {}'.format(opts.bonds_as_doubles))

    idxfunc = lambda a: a.GetAtomMapNum()
    bond_types = [Chem.rdchem.BondType.SINGLE, Chem.rdchem.BondType.DOUBLE, Chem.rdchem.BondType.TRIPLE,
                  Chem.rdchem.BondType.AROMATIC]
    bond_types_as_double = {0.0: 0, 1.0: 1, 2.0: 2, 3.0: 3, 1.5: 4}

    # Define a standardization procedure so we can evaluate based on...
    # a) RDKit-sanitized equivalence, and
    # b) MOLVS-sanitized equivalence
    from molvs import Standardizer
    standardizer = Standardizer()
    standardizer.prefer_organic = True
    def sanitize_smiles(smi, largest_fragment=False):
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            return smi
        try:
            mol = standardizer.standardize(mol) # standardize functional group reps
            if largest_fragment:
                mol = standardizer.largest_fragment(mol) # remove product counterions/salts/etc.
            mol = standardizer.uncharge(mol) # neutralize, e.g., carboxylic acids
        except Exception:
            pass
        return Chem.MolToSmiles(mol)


    try:
        rank = []
        n, top1, top2, top3, top5, gold_found = 0, 0, 0, 0, 0, 0
        top1_sani, top2_sani, top3_sani, top5_sani, gold_found_sani = 0, 0, 0, 0, 0
        for predict_edits in file_predict:
            thisrow = []
            predict_edits = predict_edits.strip('\r\n |')
            gold = file_gold.readline()
            reaction, gold_edits = gold.split()
            reactant, _, true_product = reaction.split('>')

            if opts.singleonly and '.' in true_product:
                continue

            reactant_mol = Chem.MolFromSmiles(reactant)
            true_product_mol = Chem.MolFromSmiles(true_product)

            thisrow.append(reactant)
            thisrow.append(true_product)

            # Save pbond information
            true_product_bonds = {}
            for bond in true_product_mol.GetBonds():
                a1 = idxfunc(bond.GetBeginAtom())
                a2 = idxfunc(bond.GetEndAtom())
                t = bond_types.index(bond.GetBondType())
                true_product_bonds[(a1, a2)] = true_product_bonds[(a2, a1)] = t + 1

            for atom in true_product_mol.GetAtoms():
                atom.ClearProp('molAtomMapNumber')

            true_product_smiles = Chem.MolToSmiles(true_product_mol)
            true_product_smiles_sani = set(sanitize_smiles(true_product_smiles, True).split('.'))
            true_product_smiles = set(true_product_smiles.split('.'))

            thisrow.append('.'.join(true_product_smiles))
            thisrow.append('.'.join(true_product_smiles_sani))


            ########### Use *true* edits to try to recover product

            if opts.bonds_as_doubles:
                cbonds = []
                for gedit in gold_edits.split(';'):
                    x,y,t = gedit.split('-')
                    x,y,t = int(x), int(y), float(t)
                    cbonds.append((x, y, bond_types_as_double[t]))
            else:
                # check if psmiles is recoverable
                cbonds = []
                for gedit in gold_edits.split(';'):
                    x,y = gedit.split('-')
                    x,y = int(x), int(y)
                    if (x,y) in true_product_bonds:
                        t = true_product_bonds[(x, y)]
                    else:
                        t = 0
                    cbonds.append((x, y, t))

            # Generate products by modifying reactants with predicted edits.
            predict_smiles = edit_mol(reactant_mol, cbonds)
            predict_smiles_sani = set(sanitize_smiles(smi) for smi in predict_smiles)
            predict_smiles = set(predict_smiles)
            if not true_product_smiles <= predict_smiles:

                # Try again with kekulized form
                Chem.Kekulize(reactant_mol)
                pred_smiles_kek = edit_mol(reactant_mol, cbonds)
                pred_smiles_kek = set(pred_smiles_kek)
                if not true_product_smiles <= pred_smiles_kek:
                    if true_product_smiles_sani <= predict_smiles_sani:
                        print('\nwarn: mismatch, but only due to standardization')
                        gold_found_sani += 1

                    else:
                        print('\nwarn: Could not regenerate product {}'.format(true_product_smiles))
                        print('sani product: {}'.format(true_product_smiles_sani))
                        print(reactant)
                        print(true_product)
                        print(gold_edits)
                        print(cbonds)
                        print('pred_smiles: {}'.format(predict_smiles))
                        print('pred_smiles_kek: {}'.format(pred_smiles_kek))
                        print('pred_smiles_sani: {}'.format(predict_smiles_sani))

                else:
                    gold_found += 1
                    gold_found_sani += 1

            else:
                gold_found += 1
                gold_found_sani += 1

            ########### Now use candidate edits to try to recover product

            true_product_rank_in_candidates, true_product_rank_in_candidates_sani = 11, 11
            predict_smiles_list = []
            predict_smiles_sani_list = []
            counter = 0
            for idx, edits in enumerate(predict_edits.split('|')):
                prev_len_pred_smiles = len(set(predict_smiles_list))
                couldnt_find_smiles = True
                cbonds = []
                for predict_edit in edits.split():
                    x,y,t = predict_edit.split('-')
                    if opts.bonds_as_doubles:
                        x,y,t = int(x), int(y), bond_types_as_double[float(t)]
                    else:
                        x,y,t = int(x),int(y),int(t)
                    cbonds.append((x,y,t))

                #Generate products by modifying reactants with predicted edits.
                predict_smiles = edit_mol(reactant_mol, cbonds)
                predict_smiles = set(predict_smiles)
                predict_smiles_sani = set(sanitize_smiles(smi) for smi in predict_smiles)

                if true_product_smiles_sani <= predict_smiles_sani:
                    true_product_rank_in_candidates_sani = min(true_product_rank_in_candidates_sani, counter + 1)
                if true_product_smiles <= predict_smiles:
                    true_product_rank_in_candidates = min(true_product_rank_in_candidates, counter + 1)
                # if (rk < 10) and (rk_sani < 10):
                #     break

                # Record unkekulized form
                predict_smiles_list.append('.'.join(predict_smiles))
                predict_smiles_sani_list.append('.'.join(predict_smiles_sani))

                #Edit molecules with reactants kekulized. Sometimes previous editing fails due to RDKit sanitization error (edited molecule cannot be kekulized)
                try:
                    Chem.Kekulize(reactant_mol)
                except Exception as e:
                    pass

                predict_smiles = edit_mol(reactant_mol, cbonds)
                # predict_smiles = set(predict_smiles)top1

                predict_smiles = set(predict_smiles)


                predict_smiles_sani = set(sanitize_smiles(smi) for smi in predict_smiles)
                if true_product_smiles_sani <= predict_smiles_sani:
                    true_product_rank_in_candidates_sani = min(true_product_rank_in_candidates_sani, counter + 1)
                if true_product_smiles <= predict_smiles:
                    true_product_rank_in_candidates = min(true_product_rank_in_candidates, counter + 1)

                # If we failed to come up with a new candidate, don't increment the counter!
                if len(set(predict_smiles_list)) > prev_len_pred_smiles:
                    counter += 1

            n += 1.0
            if true_product_rank_in_candidates == 1: top1 += 1
            if true_product_rank_in_candidates <= 2: top2 += 1
            if true_product_rank_in_candidates <= 3: top3 += 1
            if true_product_rank_in_candidates <= 5: top5 += 1
            if true_product_rank_in_candidates_sani == 1: top1_sani += 1
            if true_product_rank_in_candidates_sani <= 2: top2_sani += 1
            if true_product_rank_in_candidates_sani <= 3: top3_sani += 1
            if true_product_rank_in_candidates_sani <= 5: top5_sani += 1

            thisrow.append(true_product_rank_in_candidates)
            while len(predict_smiles_list) < 10:
                predict_smiles_list.append('n/a')
            thisrow.extend(predict_smiles_list)
            thisrow.append(true_product_rank_in_candidates_sani)
            while len(predict_smiles_sani_list) < 10:
                predict_smiles_sani_list.append('n/a')
            thisrow.extend(predict_smiles_sani_list)

            print('Finished number is: %.0f;' % (n))
            print('Number of top1 is: %.0f;' % (top1_sani) )
            # print(true_product_smiles)
            # print (predict_smiles_list)
            print(true_product_rank_in_candidates)  # !!
            print('[Strict Compare]  acc@1: %.4f, acc@2: %.4f, acc@3: %.4f, acc@5: %.4f (after seeing %d) gfound = %.4f' % (top1 / n, top2 / n, top3 / n, top5 / n, n, gold_found / n))
            print('[Standrized Smiles Compare]   acc@1: %.4f, acc@2: %.4f, acc@3: %.4f, acc@5: %.4f (after seeing %d) gfound = %.4f' % (top1_sani / n, top2_sani / n, top3_sani / n, top5_sani / n, n, gold_found_sani / n))
            file_eval.write('\t'.join([str(x) for x in thisrow]) + '\n')

        time_end = time.time()
        time_c = time_end - time_start
        print("Times cost(secs): ")
        print(time_c)


    finally:


        file_predict.close()
        file_gold.close()
        file_eval.close()
