
/*============================================================================
 * Name        : EmployeeLegPreferences.cpp
 * Author      : Frederic Quesnel
 * Version     : 1.0
 * Description : Randomly assign airlegs to fictive employees for multiple instances at once.
 *				 The airlegs are extracted from the file "initialSolution.in", placed in each instance folder.
 *				 "initialSolution.in" contains all the airleg, grouped in pairings,and each pairing is assigned
 *				 to a base. To each employee is assigned fixed percentage of all airlegs from the pairings related to his base.
 *				 This percentage is definded by the variable in the parameter file, specified in the arguments.
 *				 The number of employees at each base is extracted from the file "listOfBases.csv", in each instance folder.
 *
 * 				 arguments : path/to/parameter/file
 *
 * For more details about how to use this program, see the README file.
==============================================================================*/

#include <iostream>
#include <iomanip>
#include <cstdlib>
#include <vector>
#include <sstream>
#include <fstream>

using std::cout;
using std::cin;
using std::ios;
using std::endl;
using std::string;
using std::vector;


int main(int argc, char* argv[]) {

	//set seed of random number generator
	srand((unsigned)time(0));

	//percentage of airlegs to be choosen
	float percentageOfChosen;

	string a;//dummy variable used to discard useless input


	//-------------------------------------------------
	//extracts arguments from the parameter file (passed as argument)


	//name of the parameter file
	std::stringstream paramsName(argv[1]);

	//open parameter file
	std::ifstream params(paramsName.str().c_str());

	//line from the parameter file
	string paramLine ;

	//list of all instance folders (relative path)
	vector<string> instanceNames;

	//skip lines until it arrives at the "instances" line
	while(getline(params,paramLine) && paramLine.compare("instances")!=0){
	}

	//extract instance names
	while(getline(params,paramLine) && !paramLine.compare("")==0){
		instanceNames.push_back(paramLine);
	}

	//skip lines until it arrives at the "EmployeeLegPreferences.cpp" line
	while(getline(params,paramLine) && !paramLine.compare("EmployeeLegPreferences.cpp")==0){
	}
	//extracts the percentage of airleg chosen for each employee
	params >> a>>a>>a>>a>>percentageOfChosen;

	//number of current instance
	int instanceNb = 0;

	//---------------------------------------------------------
	// for each instance, generate the preferred airlegs for each employee
	while(instanceNb < (int) instanceNames.size()){

		//path to the file "listOfBases.csv"
		std::stringstream inName;
		inName <<instanceNames[instanceNb]<<"/listOfBases.csv";
		//opens the file containing the list of bases (used to count the number of employees)
		std::ifstream bases(inName.str().c_str(),ios::in);

		//if listOfBases.csv is not found
		if(bases.fail()){
			cout<<"Error : could not open listOfBases.csv in folder : "<<instanceNames[instanceNb]<<"  .  Proceeding with next instance..."<<endl;

		}
		//listOfBases.csv is open
		else{

			//----------------------------------------------------------------
			//extracts the base names and the number of employee at each base from listOfBases.csv

			//count the number of employees per base (3th column in file "listOfBases.csv")
			vector<int> nbEmployeesPerBase;

			//vector containing the base names
			vector<string> airportNames ;
			string baseName;

			//ignore first line
			getline(bases,a);

			//line of listOfBases.csv file
			string line;

			//for each base, read base name and number of employees
			while(getline(bases,line)){
				//if the line is not empty (or only white spaces)
				if(line.find_first_not_of(' ') != std::string::npos){

					//extract name of airport
					string strAirport = line.substr(0,line.find_first_of(" "));


					//add airport name to the list
					airportNames.push_back(strAirport);

					//extract the number of employees from the line
					int emp = atoi(line.substr(line.find_last_of(",")+1).c_str());

					//add the number of employees to the list
					nbEmployeesPerBase.push_back(emp);
				}
			}
			//close listOfBases
			bases.close();

			//create a 2D vector containing all airlegs per base.
			vector< vector<string> > legsPerBase;

			//create a new vector for each base
			for(int i = 0 ; i< (int) airportNames.size();i++){
				legsPerBase.push_back(vector<string>(0));
			}


			//-----------------------------------------------------------------------
			// extracts the pairings from initialSolution.in

			//path and name for initialSolution.in for the current instance
			std::stringstream initialSolutionFileName;
			initialSolutionFileName << instanceNames[instanceNb]<<"/initialSolution.in";
			//open initialSolution.in
			std::ifstream initialSolution(initialSolutionFileName.str().c_str() , ios::in);

			//if the programm fails to open initialSolution.in, display an error message and proceed with the next instance
			if(initialSolution.fail()){
				cout<<"Error : file initialSolution.in not found in instance folder "<<instanceNames[instanceNb]<<"proceeding with next instance"<<endl;
			}
			else{
				//discard first line (header)
				getline(initialSolution,a);

				//discard second line (blank line)
				getline(initialSolution,a);

				//for each line (corresponding to one pairing), extract the base name
				while(initialSolution >> a >> a >> a >> a >> baseName){

					int indexBase = -1;
					//find the index of the base in baseNames corresponding to the pairing
					for(int i = 0 ; i < (int) airportNames.size();i++){
						//if match found
						if(airportNames[i].compare(baseName)==0){
							indexBase = i;
							break;
						}
					}

					//string corresponding to one airleg in the file
					string leg;
					//determine if last airleg of line (necessary to remove last ";" and to know when to change line)
					bool lastLegOfLine = false;

					//extracts each leg from the line (pairing)
					while(!lastLegOfLine && initialSolution >>a >> leg){

						//if ";" is not found, pos == string::npos
						size_t pos = leg.find_last_of(";");

						//if last leg of the line
						if(pos != string::npos){
							lastLegOfLine = true;
							//remove the ";"
							leg = leg.substr(0,leg.length()-1);
						}

						//legs starting with TDH are not included (they are deadheads)
						//if leg does not start with "TDH", add leg to the list corresponding to the base of the pairing
						if(leg.substr(0,3).compare("TDH")!=0){
							legsPerBase[indexBase].push_back(leg);
						}

					}
					// 1/2 lines are blank in initialSolution.in
					getline(initialSolution,a);

				}

				//-----------------------------------------------------
				//choose the preferred airlegs and create the output file

				//name of output file
				std::stringstream outName;
				outName<<instanceNames[instanceNb]<<"/PreferredAirLegs.csv";

				//open output file
				std::ofstream fileout;
				fileout.open(outName.str().c_str(),ios::out | ios::trunc);

				//if program fails to open output file, display an error message and proceed with next instance
				if(fileout.fail()){
					cout << "Error : could not open output file PreferredAirlegs.txt in instance"<<instanceNames[instanceNb]<<". Proceeding with next instance."<<endl;
				}
				else{

					//first line of the file
					fileout << "employee , legs"<< endl;

					//number of the employee whose preferences are currently chosen
					int employeeNumber = 0;
					//for each base
					for(int j = 0 ; j<  (int) airportNames.size();j++){
						//total number of airlegs in base
						int nbLegs = legsPerBase[j].size();
						//for each employee in the base, selects "percentageOfChosen" percent of airlegs
						for(int i = 0;i < nbEmployeesPerBase[j] ; i++){
							//increment employee number
							employeeNumber++;

							//copy legs vector for this base
							//when an airleg is chosen, it is removed from the vector "tempLegs" so it is not chosen twice.
							vector<string> tempLegs = legsPerBase[j];

							//writes the employe number (displays correctly for 1 to 999 employees.  If more, the display will be off 1 space)
							if(employeeNumber <10){
								fileout << "EMP00" << employeeNumber <<" , ";
							}
							else if(employeeNumber < 100){
								fileout << "EMP0"  << employeeNumber <<" , ";
							}
							else{
								fileout << "EMP"   << employeeNumber <<" , ";
							}

							//number of airlegs to be randomly picked
							int numberOfChosen = nbLegs*percentageOfChosen/100.;

							//choose "percentageOfChosen"% of legs and assign them to the employee
							for(int k = 0;k<numberOfChosen;k++){
								//index of chosen leg
								int chosenLeg  = rand() % tempLegs.size();

								//writes the leg to the file (there is no comma after the last leg)
								if(k != numberOfChosen-1){
									fileout << tempLegs[chosenLeg] << " , ";
								}
								else{
									//if last leg
									fileout << tempLegs[chosenLeg];
								}
								//remove leg from temporary list so it is not chosen twice
								tempLegs.erase(tempLegs.begin() + chosenLeg);
							}
							//end of preference for the current employee
							fileout<<endl;
						}
					}
					//close file
					fileout.close();
				}
			}
		}
		//next instance
		instanceNb++;
	}
	return(0);
}
